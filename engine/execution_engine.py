"""
高频订单执行引擎

设计模式: Command Pattern + Queue
负责管理订单生命周期、队列调度和事件发布。
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Callable

from connectors.base import BaseConnector, OrderResult, OrderSide, OrderType
from connectors.retry import retry_async, RetryConfig, NonceManager
from core.exceptions import (
    OrderExecutionError,
    OrderTimeoutError,
    NonceConflictError,
)
from engine.event_bus import EventBus, Event, EventType
from strategies.base import Signal, SignalAction

logger = logging.getLogger(__name__)


# ==================== 订单状态机 ====================

class OrderState(str, Enum):
    """订单状态"""
    PENDING = "pending"      # 等待执行
    SUBMITTING = "submitting"  # 提交中
    SUBMITTED = "submitted"    # 已提交
    FILLED = "filled"          # 已成交
    CANCELLED = "cancelled"    # 已取消
    FAILED = "failed"          # 失败
    TIMEOUT = "timeout"        # 超时


# ==================== 订单任务 ====================

@dataclass(order=True)
class OrderTask:
    """
    待执行订单任务
    
    实现 __lt__ 用于优先级队列排序。
    priority 值越小优先级越高。
    """
    priority: int
    id: str = field(compare=False)
    signal: Signal = field(compare=False)
    symbol: str = field(compare=False)
    size: float = field(compare=False)
    price: Optional[float] = field(default=None, compare=False)
    
    state: OrderState = field(default=OrderState.PENDING, compare=False)
    order_id: Optional[str] = field(default=None, compare=False)  # 交易所返回的 ID
    result: Optional[OrderResult] = field(default=None, compare=False)
    
    created_at: datetime = field(default_factory=datetime.now, compare=False)
    timeout: float = field(default=10.0, compare=False)  # 秒
    retries: int = field(default=0, compare=False)
    max_retries: int = field(default=3, compare=False)
    
    def to_global_id(self) -> str:
        """生成 Global Order ID"""
        ts = int(self.created_at.timestamp())
        side = "BUY" if self.signal.action == SignalAction.BUY else "SELL"
        return f"ORD_{side}_{ts}_{id(self) % 10000}"


# ==================== 执行引擎 ====================

class ExecutionEngine:
    """
    高频订单执行引擎
    
    功能:
    - 异步订单队列 (优先级队列)
    - 订单状态机跟踪
    - 并发执行限制
    - 自动超时取消
    - 事件发布集成
    - 风控集成 (RiskManager)
    - 自动内存清理 (防止 OOM)
    
    使用示例:
    ```python
    engine = ExecutionEngine(connector, event_bus)
    await engine.start()
    
    order_id = await engine.submit(signal, symbol="ETH-USDC", size=0.1)
    
    # 等待完成
    result = await engine.wait_for(order_id)
    
    await engine.stop()
    ```
    """
    
    def __init__(
        self,
        connector: BaseConnector,
        event_bus: Optional[EventBus] = None,
        max_concurrent: int = 5,
        nonce_manager: Optional[NonceManager] = None,
        risk_manager: Optional["RiskManager"] = None,
        account_ws: Optional["AccountWebSocket"] = None,  # 账户 WebSocket
        task_ttl_seconds: float = 3600,  # 订单任务 TTL (默认 1 小时)
        cleanup_interval_seconds: float = 300,  # 清理间隔 (默认 5 分钟)
    ):
        self.connector = connector
        self.event_bus = event_bus
        self.max_concurrent = max_concurrent
        self.nonce_manager = nonce_manager or NonceManager()
        self.risk_manager = risk_manager  # 可选风控模块
        self.account_ws = account_ws  # 账户 WebSocket (监听真实成交)
        
        # 内存管理配置
        self._task_ttl = task_ttl_seconds
        self._cleanup_interval = cleanup_interval_seconds
        
        self._queue: asyncio.PriorityQueue[OrderTask] = asyncio.PriorityQueue()
        self._tasks: Dict[str, OrderTask] = {}  # 所有已提交任务
        self._pending: Dict[str, OrderTask] = {}  # 执行中的任务
        self._completed: Dict[str, OrderTask] = {}  # 已完成任务
        
        # 交易所订单 ID -> 内部任务 ID 映射
        self._exchange_order_map: Dict[str, str] = {}
        
        self._workers: list[asyncio.Task] = []
        self._cleanup_task: Optional[asyncio.Task] = None  # 后台清理任务
        self._running = False
        self._semaphore: Optional[asyncio.Semaphore] = None
        
        # 回调
        self._on_order_complete: Optional[Callable[[OrderTask], None]] = None
        
        # 注册账户 WS 回调
        if self.account_ws:
            self.account_ws.on_fill(self._on_ws_fill)
            self.account_ws.on_order_update(self._on_ws_order_update)

    
    # ==================== 生命周期 ====================
    
    async def start(self) -> None:
        """启动执行引擎"""
        if self._running:
            return
        
        self._running = True
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        
        # 启动工作协程
        for i in range(self.max_concurrent):
            worker = asyncio.create_task(self._worker(i))
            self._workers.append(worker)
        
        # 启动后台清理任务
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        
        logger.info(f"ExecutionEngine 已启动 ({self.max_concurrent} workers, TTL={self._task_ttl}s)")
        
        if self.event_bus:
            await self.event_bus.publish(Event(
                event_type=EventType.SYSTEM_START,
                data={"component": "ExecutionEngine"},
                source="execution_engine"
            ))
    
    async def stop(self) -> None:
        """停止执行引擎"""
        self._running = False
        
        # 取消清理任务
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        
        # 取消所有 worker
        for worker in self._workers:
            worker.cancel()
        
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        
        logger.info("ExecutionEngine 已停止")
        
        if self.event_bus:
            await self.event_bus.publish(Event(
                event_type=EventType.SYSTEM_STOP,
                data={"component": "ExecutionEngine"},
                source="execution_engine"
            ))
    
    # ==================== 订单提交 ====================
    
    async def submit(
        self,
        signal: Signal,
        symbol: str,
        size: float,
        price: Optional[float] = None,
        priority: int = 1,
        timeout: float = 10.0,
    ) -> str:
        """
        提交订单到队列
        
        Args:
            signal: 交易信号
            symbol: 交易对
            size: 订单数量
            price: 限价 (None 则使用市价)
            priority: 优先级 (1=最高, 数值越大优先级越低)
            timeout: 超时时间 (秒)
        
        Returns:
            订单 Global ID
        
        Raises:
            RiskException: 风控检查未通过
        """
        # 风控检查 (如果配置了 RiskManager)
        if self.risk_manager:
            side_str = "BUY" if signal.action == SignalAction.BUY else "SELL"
            order_price = price or signal.price
            self.risk_manager.check_order(symbol, side_str, size, order_price)
        
        task = OrderTask(
            priority=priority,
            id="",  # 稍后生成
            signal=signal,
            symbol=symbol,
            size=size,
            price=price or signal.price,
            timeout=timeout,
        )
        task.id = task.to_global_id()
        
        self._tasks[task.id] = task
        await self._queue.put(task)
        
        logger.info(f"订单已入队: {task.id} | {signal.action.value} {size} {symbol}")
        
        return task.id
    
    async def cancel(self, order_id: str) -> bool:
        """
        取消订单
        
        Args:
            order_id: 订单 Global ID
        
        Returns:
            是否成功取消
        """
        task = self._tasks.get(order_id)
        if not task:
            logger.warning(f"订单不存在: {order_id}")
            return False
        
        # 如果还在队列中，标记取消
        if task.state == OrderState.PENDING:
            task.state = OrderState.CANCELLED
            logger.info(f"订单已取消 (未执行): {order_id}")
            return True
        
        # 如果已提交，调用交易所取消
        if task.state in (OrderState.SUBMITTING, OrderState.SUBMITTED):
            if task.order_id:
                success = await self.connector.cancel_order(task.order_id)
                if success:
                    task.state = OrderState.CANCELLED
                    logger.info(f"订单已取消: {order_id}")
                return success
        
        return False
    
    async def wait_for(self, order_id: str, timeout: float = 30) -> Optional[OrderResult]:
        """
        等待订单完成
        
        Args:
            order_id: 订单 Global ID
            timeout: 最大等待时间
        
        Returns:
            OrderResult 或 None (超时)
        """
        start = asyncio.get_event_loop().time()
        
        while (asyncio.get_event_loop().time() - start) < timeout:
            task = self._tasks.get(order_id)
            if task and task.state in (
                OrderState.FILLED, 
                OrderState.CANCELLED, 
                OrderState.FAILED,
                OrderState.TIMEOUT,
            ):
                return task.result
            
            await asyncio.sleep(0.1)
        
        return None
    
    # ==================== 内部方法 ====================
    
    async def _worker(self, worker_id: int) -> None:
        """工作协程 - 从队列消费并执行订单"""
        logger.debug(f"Worker-{worker_id} 已启动")
        
        while self._running:
            try:
                # 等待队列任务
                task = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            
            # 检查任务是否已取消
            if task.state == OrderState.CANCELLED:
                self._queue.task_done()
                continue
            
            # 执行订单
            async with self._semaphore:
                self._pending[task.id] = task
                try:
                    await self._execute_order(task)
                finally:
                    self._pending.pop(task.id, None)
                    self._completed[task.id] = task
                    self._queue.task_done()
        
        logger.debug(f"Worker-{worker_id} 已停止")
    
    async def _execute_order(self, task: OrderTask) -> None:
        """执行单笔订单"""
        task.state = OrderState.SUBMITTING
        
        # 确定订单类型
        order_type = OrderType.MARKET
        if task.price and task.price > 0:
            order_type = OrderType.LIMIT
        
        # 确定方向
        side = OrderSide.BUY if task.signal.action == SignalAction.BUY else OrderSide.SELL
        
        try:
            # 带重试的订单提交
            result = await retry_async(
                lambda: self.connector.create_order(
                    symbol=task.symbol,
                    side=side,
                    order_type=order_type,
                    size=task.size,
                    price=task.price,
                ),
                config=RetryConfig(
                    max_retries=task.max_retries,
                    base_delay=0.2,
                ),
                on_retry=lambda e, attempt: logger.warning(
                    f"订单重试 {attempt + 1}: {task.id}"
                ),
            )
            
            task.result = result
            
            if result.success:
                task.order_id = result.order_id
                task.state = OrderState.SUBMITTED
                logger.info(f"✅ 订单已提交: {task.id} -> {result.order_id}")
                
                # 保存交易所订单 ID 映射
                if result.order_id:
                    self._exchange_order_map[result.order_id] = task.id
                
                # 发布事件
                await self._publish_event(EventType.ORDER_CREATED, task)
                
                # 如果有账户 WebSocket，等待真实成交通知
                # 否则假设提交即成交 (测试网或无 WS 模式)
                if self.account_ws and self.account_ws.is_running:
                    logger.debug(f"等待 WS 成交通知: {task.order_id}")
                    # 状态保持 SUBMITTED，由 _on_ws_fill 回调更新
                else:
                    # 无 WS 模式：假设提交即成交
                    task.state = OrderState.FILLED
                    await self._publish_event(EventType.ORDER_FILLED, task)
                    
                    # 更新风险状态
                    if self.risk_manager:
                        try:
                            fill_data = {
                                "symbol": task.symbol,
                                "side": side.value,
                                "quantity": task.size,
                                "price": task.result.average_price or task.price or 0,
                                "fee": task.result.fee or 0.0
                            }
                            self.risk_manager.on_fill(fill_data)
                        except Exception as e:
                            logger.error(f"风险状态更新失败: {e}")
            else:
                task.state = OrderState.FAILED
                logger.error(f"❌ 订单失败: {task.id} - {result.error}")
                await self._publish_event(EventType.ORDER_FAILED, task)
        
        except OrderTimeoutError:
            task.state = OrderState.TIMEOUT
            logger.error(f"⏰ 订单超时: {task.id}")
            await self._publish_event(EventType.ORDER_FAILED, task)
        
        except Exception as e:
            task.state = OrderState.FAILED
            task.result = OrderResult.fail(str(e))
            logger.exception(f"订单执行异常: {task.id}")
            await self._publish_event(EventType.ORDER_FAILED, task)
        
        # 触发回调
        if self._on_order_complete:
            try:
                self._on_order_complete(task)
            except Exception:
                pass
    
    async def _publish_event(self, event_type: EventType, task: OrderTask) -> None:
        """发布订单事件"""
        if not self.event_bus:
            return
        
        await self.event_bus.publish(Event(
            event_type=event_type,
            data={
                "order_id": task.id,
                "exchange_order_id": task.order_id,
                "symbol": task.symbol,
                "side": task.signal.action.value,
                "size": task.size,
                "price": task.price,
                "state": task.state.value,
                "error": task.result.error if task.result else None,
            },
            source="execution_engine"
        ))
    
    # ==================== 内存管理 ====================
    
    async def _cleanup_loop(self) -> None:
        """后台清理循环 - 定期清除过期订单任务"""
        logger.info(f"清理任务已启动 (间隔={self._cleanup_interval}s, TTL={self._task_ttl}s)")
        
        while self._running:
            try:
                await asyncio.sleep(self._cleanup_interval)
                cleaned = self._cleanup_expired_tasks()
                if cleaned > 0:
                    logger.info(f"🧹 已清理 {cleaned} 个过期订单任务")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理任务异常: {e}")
        
        logger.debug("清理任务已停止")
    
    def _cleanup_expired_tasks(self) -> int:
        """
        清除过期订单任务 (防止内存泄漏)
        
        Returns:
            清理的任务数量
        """
        now = datetime.now()
        to_remove = []
        
        # 清理已完成任务
        for order_id, task in self._completed.items():
            age_seconds = (now - task.created_at).total_seconds()
            if age_seconds > self._task_ttl:
                to_remove.append(order_id)
        
        # 执行删除
        for order_id in to_remove:
            task = self._completed.get(order_id)
            del self._completed[order_id]
            
            # 同时从 _tasks 中移除
            if order_id in self._tasks:
                del self._tasks[order_id]
            
            # 清理 _exchange_order_map (防止内存泄漏)
            if task and task.order_id:
                self._exchange_order_map.pop(task.order_id, None)
        
        return len(to_remove)
    
    def force_cleanup(self) -> int:
        """手动触发清理 (用于调试)"""
        return self._cleanup_expired_tasks()
    
    # ==================== 状态查询 ====================
    
    def get_pending_count(self) -> int:
        """获取执行中订单数量"""
        return len(self._pending)
    
    def get_queue_size(self) -> int:
        """获取队列长度"""
        return self._queue.qsize()
    
    def get_task(self, order_id: str) -> Optional[OrderTask]:
        """获取订单任务"""
        return self._tasks.get(order_id)
    
    def get_stats(self) -> dict:
        """获取引擎统计"""
        states = {}
        for task in self._tasks.values():
            state = task.state.value
            states[state] = states.get(state, 0) + 1
        
        return {
            "total_tasks": len(self._tasks),
            "queue_size": self._queue.qsize(),
            "pending": len(self._pending),
            "completed": len(self._completed),
            "by_state": states,
            "running": self._running,
        }
    
    def set_on_complete(self, callback: Callable[[OrderTask], None]) -> None:
        """设置订单完成回调"""
        self._on_order_complete = callback
    
    # ==================== WebSocket 回调 ====================
    
    def _on_ws_fill(self, fill: "FillEvent") -> None:
        """处理 WebSocket 成交通知"""
        order_id = str(fill.order_index)
        task_id = self._exchange_order_map.get(order_id)
        
        if not task_id:
            logger.debug(f"未知订单成交: {order_id}")
            return
        
        task = self._tasks.get(task_id)
        if not task:
            return
        
        # 更新任务状态
        task.state = OrderState.FILLED
        if task.result:
            task.result.average_price = fill.price
            task.result.fee = fill.fee
        
        logger.info(f"📈 WS 成交确认: {task.id} @ {fill.price}")
        
        # 更新风控状态
        if self.risk_manager:
            try:
                # 获取 symbol (需要从 market_id 反查)
                symbol = task.symbol
                side = "BUY" if task.signal.action.value == "buy" else "SELL"
                
                fill_data = {
                    "symbol": symbol,
                    "side": side,
                    "quantity": fill.size,
                    "price": fill.price,
                    "fee": fill.fee
                }
                self.risk_manager.on_fill(fill_data)
            except Exception as e:
                logger.error(f"风控更新失败: {e}")
        
        # 发布事件 (需要在事件循环中执行)
        if self.event_bus:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(self._publish_event(EventType.ORDER_FILLED, task))
            except:
                pass
    
    def _on_ws_order_update(self, update: "OrderUpdate") -> None:
        """处理 WebSocket 订单状态更新"""
        order_id = str(update.order_index)
        task_id = self._exchange_order_map.get(order_id)
        
        if not task_id:
            return
        
        task = self._tasks.get(task_id)
        if not task:
            return
        
        # 更新状态
        if update.status == "cancelled":
            task.state = OrderState.CANCELLED
            logger.info(f"订单已取消: {task.id}")
        elif update.status == "filled":
            task.state = OrderState.FILLED


# ==================== 便捷函数 ====================

_engine_instance: Optional[ExecutionEngine] = None


def get_execution_engine(
    connector: Optional[BaseConnector] = None,
    event_bus: Optional[EventBus] = None,
) -> ExecutionEngine:
    """
    获取或创建执行引擎单例
    
    首次调用必须提供 connector。
    """
    global _engine_instance
    
    if _engine_instance is None:
        if connector is None:
            raise ValueError("首次调用必须提供 connector")
        _engine_instance = ExecutionEngine(connector, event_bus)
    
    return _engine_instance
