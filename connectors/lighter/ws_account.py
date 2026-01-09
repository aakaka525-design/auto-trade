"""
账户 WebSocket 订阅器

监听账户变动事件，实时跟踪订单成交状态。
"""
import logging
import threading
from typing import Dict, Optional, Callable, Any
from datetime import datetime
from dataclasses import dataclass

import lighter

logger = logging.getLogger(__name__)


@dataclass
class OrderUpdate:
    """订单状态更新"""
    order_index: int
    market_id: int
    status: str  # "open", "filled", "cancelled"
    filled_size: float
    remaining_size: float
    avg_fill_price: float
    timestamp: datetime


@dataclass
class FillEvent:
    """成交事件"""
    order_index: int
    market_id: int
    side: str
    price: float
    size: float
    fee: float
    timestamp: datetime


class AccountWebSocket:
    """
    账户 WebSocket 订阅器
    
    监听账户变动，实时推送订单状态和成交事件。
    
    使用示例:
    ```python
    ws = AccountWebSocket(account_index=12345)
    ws.on_fill(lambda fill: print(f"成交: {fill}"))
    ws.on_order_update(lambda order: print(f"订单更新: {order}"))
    ws.start()
    ```
    """
    
    def __init__(
        self,
        account_index: int,
        host: str = "mainnet.zklighter.elliot.ai",
    ):
        self._account_index = account_index
        self._host = host
        
        # 回调
        self._on_fill_callback: Optional[Callable[[FillEvent], None]] = None
        self._on_order_callback: Optional[Callable[[OrderUpdate], None]] = None
        
        # 状态
        self._orders: Dict[int, OrderUpdate] = {}
        self._lock = threading.Lock()
        
        # WebSocket
        self._ws_client: Optional[lighter.WsClient] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
    
    def on_fill(self, callback: Callable[[FillEvent], None]) -> None:
        """注册成交回调"""
        self._on_fill_callback = callback
    
    def on_order_update(self, callback: Callable[[OrderUpdate], None]) -> None:
        """注册订单状态回调"""
        self._on_order_callback = callback
    
    def start(self) -> None:
        """启动 WebSocket 订阅"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        logger.info(f"账户 WebSocket 已启动: account={self._account_index}")
    
    def stop(self) -> None:
        """停止订阅"""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.close()
            except:
                pass
        logger.info("账户 WebSocket 已停止")
    
    def _run_ws(self) -> None:
        """在后台线程运行 WebSocket (带自动重连)"""
        reconnect_count = 0
        max_reconnects = 10
        
        while self._running and reconnect_count < max_reconnects:
            try:
                self._ws_client = lighter.WsClient(
                    host=self._host,
                    order_book_ids=[],
                    account_ids=[self._account_index],
                    on_account_update=self._on_account_update,
                )
                logger.info(f"账户 WebSocket 已连接: {self._host}")
                reconnect_count = 0
                self._ws_client.run()
                
            except Exception as e:
                reconnect_count += 1
                if self._running:
                    wait_time = min(2 ** reconnect_count, 30)
                    logger.warning(f"账户 WS 断开，{wait_time}s 后重连 ({reconnect_count}/{max_reconnects}): {e}")
                    import time
                    time.sleep(wait_time)
        
        if reconnect_count >= max_reconnects:
            logger.error("账户 WebSocket 重连次数耗尽")
        self._running = False
    
    def _on_account_update(self, account_index: int, data: dict) -> None:
        """处理账户更新事件"""
        try:
            # 处理订单更新
            orders = data.get("orders", [])
            for order_data in orders:
                self._process_order_update(order_data)
            
            # 处理成交
            fills = data.get("fills", [])
            for fill_data in fills:
                self._process_fill(fill_data)
                
        except Exception as e:
            logger.error(f"账户更新处理错误: {e}")
    
    def _process_order_update(self, data: dict) -> None:
        """处理订单状态更新"""
        order_index = int(data.get("order_index", 0))
        
        update = OrderUpdate(
            order_index=order_index,
            market_id=int(data.get("market_id", 0)),
            status=data.get("status", "unknown"),
            filled_size=float(data.get("filled_size", 0)),
            remaining_size=float(data.get("remaining_size", 0)),
            avg_fill_price=float(data.get("avg_fill_price", 0)),
            timestamp=datetime.now(),
        )
        
        with self._lock:
            self._orders[order_index] = update
        
        logger.debug(f"订单更新: {order_index} -> {update.status}")
        
        if self._on_order_callback:
            try:
                self._on_order_callback(update)
            except Exception as e:
                logger.error(f"订单回调错误: {e}")
    
    def _process_fill(self, data: dict) -> None:
        """处理成交事件"""
        fill = FillEvent(
            order_index=int(data.get("order_index", 0)),
            market_id=int(data.get("market_id", 0)),
            side=data.get("side", "unknown"),
            price=float(data.get("price", 0)),
            size=float(data.get("size", 0)),
            fee=float(data.get("fee", 0)),
            timestamp=datetime.now(),
        )
        
        logger.info(f"成交: order={fill.order_index}, {fill.side} {fill.size} @ {fill.price}")
        
        if self._on_fill_callback:
            try:
                self._on_fill_callback(fill)
            except Exception as e:
                logger.error(f"成交回调错误: {e}")
    
    def get_order_status(self, order_index: int) -> Optional[OrderUpdate]:
        """获取订单最新状态"""
        with self._lock:
            return self._orders.get(order_index)
    
    @property
    def is_running(self) -> bool:
        return self._running and self._thread and self._thread.is_alive()


# 全局实例
_account_ws: Optional[AccountWebSocket] = None


def get_account_ws(account_index: int = None) -> Optional[AccountWebSocket]:
    """获取账户 WebSocket 实例"""
    global _account_ws
    if _account_ws is None and account_index:
        _account_ws = AccountWebSocket(account_index=account_index)
    return _account_ws
