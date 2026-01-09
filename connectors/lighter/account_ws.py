"""
账户 WebSocket

监听账户的订单成交、状态更新等事件。
"""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, List

import lighter

logger = logging.getLogger(__name__)


@dataclass
class FillEvent:
    """成交事件"""
    order_index: int
    market_id: int
    side: str  # "buy" / "sell"
    price: float
    size: float
    fee: float
    timestamp: datetime


@dataclass
class OrderUpdate:
    """订单状态更新"""
    order_index: int
    market_id: int
    status: str  # "open", "filled", "cancelled", "partial"
    filled_size: float
    remaining_size: float
    timestamp: datetime


class AccountWebSocket:
    """
    账户 WebSocket 管理器
    
    监听账户的成交、订单更新等事件，用于确认订单状态。
    
    使用示例:
    ```python
    ws = AccountWebSocket(
        host="mainnet.zklighter.elliot.ai",
        account_id=12345,
    )
    ws.on_fill(lambda fill: print(f"成交: {fill}"))
    ws.on_order_update(lambda update: print(f"更新: {update}"))
    ws.start()
    ```
    """
    
    def __init__(
        self,
        host: str = "mainnet.zklighter.elliot.ai",
        account_id: int = 0,
    ):
        self._host = host
        self._account_id = account_id
        
        # 回调
        self._on_fill_callbacks: List[Callable[[FillEvent], None]] = []
        self._on_order_update_callbacks: List[Callable[[OrderUpdate], None]] = []
        
        # 状态
        self._running = False
        self._ws_client = None
        self._thread: Optional[threading.Thread] = None
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    def on_fill(self, callback: Callable[[FillEvent], None]) -> None:
        """注册成交回调"""
        self._on_fill_callbacks.append(callback)
    
    def on_order_update(self, callback: Callable[[OrderUpdate], None]) -> None:
        """注册订单更新回调"""
        self._on_order_update_callbacks.append(callback)
    
    def start(self) -> None:
        """启动 WebSocket 监听"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        
        logger.info(f"账户 WebSocket 已启动: account_id={self._account_id}")
    
    def stop(self) -> None:
        """停止 WebSocket"""
        self._running = False
        logger.info("账户 WebSocket 已停止")
    
    def _run_ws(self) -> None:
        """运行 WebSocket (带自动重连)"""
        reconnect_count = 0
        max_reconnects = 10
        
        while self._running and reconnect_count < max_reconnects:
            try:
                self._ws_client = lighter.WsClient(
                    host=self._host,
                    order_book_ids=[],  # 不订阅订单簿
                    account_ids=[self._account_id],  # 订阅账户
                    on_account_update=self._on_account_update,  # 统一回调
                )
                
                logger.info(f"账户 WebSocket 已连接: {self._host}")
                reconnect_count = 0
                self._ws_client.run()  # 阻塞
                
            except Exception as e:
                reconnect_count += 1
                if self._running:
                    wait_time = min(2 ** reconnect_count, 30)
                    logger.warning(
                        f"账户 WS 断开，{wait_time}s 后重连 "
                        f"({reconnect_count}/{max_reconnects}): {e}"
                    )
                    import time
                    time.sleep(wait_time)
        
        if reconnect_count >= max_reconnects:
            logger.error("账户 WebSocket 重连次数耗尽")
        self._running = False
    
    def _on_account_update(self, account_id: int, data: dict) -> None:
        """处理账户更新 (统一入口)"""
        if account_id != self._account_id:
            return
        
        logger.debug(f"账户更新: {data}")
        
        # 解析订单更新
        orders = data.get("orders", []) if isinstance(data, dict) else []
        for order in orders:
            self._process_order_update(order)
        
        # 解析成交
        fills = data.get("fills", []) if isinstance(data, dict) else []
        for fill in fills:
            self._process_fill(fill)
    
    def _process_order_update(self, order: dict) -> None:
        """处理单个订单更新"""
        try:
            update = OrderUpdate(
                order_index=int(order.get("order_index", 0)),
                market_id=int(order.get("market_id", 0)),
                status=order.get("status", "unknown"),
                filled_size=float(order.get("filled_size", 0)),
                remaining_size=float(order.get("remaining_size", 0)),
                timestamp=datetime.now(),
            )
            
            logger.info(f"📋 订单更新: order={update.order_index} status={update.status}")
            
            for callback in self._on_order_update_callbacks:
                try:
                    callback(update)
                except Exception as e:
                    logger.error(f"订单更新回调错误: {e}")
        except Exception as e:
            logger.error(f"解析订单更新失败: {e}")
    
    def _process_fill(self, fill: dict) -> None:
        """处理单个成交"""
        try:
            event = FillEvent(
                order_index=int(fill.get("order_index", 0)),
                market_id=int(fill.get("market_id", 0)),
                side=fill.get("side", "unknown"),
                price=float(fill.get("price", 0)),
                size=float(fill.get("size", 0)),
                fee=float(fill.get("fee", 0)),
                timestamp=datetime.now(),
            )
            
            logger.info(f"📈 成交: order={event.order_index} price={event.price} size={event.size}")
            
            for callback in self._on_fill_callbacks:
                try:
                    callback(event)
                except Exception as e:
                    logger.error(f"成交回调错误: {e}")
        except Exception as e:
            logger.error(f"解析成交数据失败: {e}")
