"""
WebSocket 订单簿管理器

通过 WebSocket 实时订阅订单簿，提供最新深度数据。
"""
import logging
import threading
from typing import Dict, Optional, Callable
from datetime import datetime
from dataclasses import dataclass

import lighter

logger = logging.getLogger(__name__)


@dataclass
class OrderBookSnapshot:
    """订单簿快照"""
    market_id: int
    timestamp: datetime
    bids: Dict[str, str]  # {price: size}
    asks: Dict[str, str]
    
    @property
    def best_bid(self) -> Optional[tuple]:
        """最佳买价"""
        if not self.bids:
            return None
        price = max(self.bids.keys(), key=float)
        return (float(price), float(self.bids[price]))
    
    @property
    def best_ask(self) -> Optional[tuple]:
        """最佳卖价"""
        if not self.asks:
            return None
        price = min(self.asks.keys(), key=float)
        return (float(price), float(self.asks[price]))
    
    @property
    def mid_price(self) -> Optional[float]:
        """中间价"""
        bid = self.best_bid
        ask = self.best_ask
        if bid and ask:
            return (bid[0] + ask[0]) / 2
        return None
    
    @property
    def spread(self) -> Optional[float]:
        """价差"""
        bid = self.best_bid
        ask = self.best_ask
        if bid and ask:
            return ask[0] - bid[0]
        return None


class WebSocketOrderBook:
    """
    WebSocket 订单簿管理器
    
    在后台线程运行 WebSocket 客户端，实时更新订单簿。
    
    使用示例:
    ```python
    ws_ob = WebSocketOrderBook(market_ids=[0, 1])
    ws_ob.start()
    
    # 获取最新订单簿
    snapshot = ws_ob.get_orderbook(0)
    print(f"Mid: {snapshot.mid_price}")
    
    ws_ob.stop()
    ```
    """
    
    def __init__(
        self,
        market_ids: list[int] = [0],
        host: str = "mainnet.zklighter.elliot.ai",
        on_update: Optional[Callable[[int, OrderBookSnapshot], None]] = None,
    ):
        self._market_ids = market_ids
        self._host = host
        self._on_update = on_update
        
        # 订单簿状态
        self._orderbooks: Dict[int, OrderBookSnapshot] = {}
        self._lock = threading.Lock()
        
        # WebSocket 客户端
        self._ws_client: Optional[lighter.WsClient] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
    
    def start(self) -> None:
        """启动 WebSocket 订阅"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._run_ws, daemon=True)
        self._thread.start()
        logger.info(f"WebSocket 订单簿已启动: markets={self._market_ids}")
    
    def stop(self) -> None:
        """停止订阅"""
        self._running = False
        if self._ws_client:
            try:
                self._ws_client.close()
            except:
                pass
        logger.info("WebSocket 订单簿已停止")
    
    def _run_ws(self) -> None:
        """在后台线程运行 WebSocket (带自动重连)"""
        reconnect_count = 0
        max_reconnects = 10
        
        while self._running and reconnect_count < max_reconnects:
            try:
                self._ws_client = lighter.WsClient(
                    host=self._host,
                    order_book_ids=self._market_ids,
                    account_ids=[],
                    on_order_book_update=self._on_order_book_update,
                )
                logger.info(f"WebSocket 已连接: {self._host}")
                reconnect_count = 0  # 成功连接，重置计数
                self._ws_client.run()  # 阻塞直到断开
                
            except Exception as e:
                reconnect_count += 1
                if self._running:
                    wait_time = min(2 ** reconnect_count, 30)
                    logger.warning(f"WebSocket 断开，{wait_time}s 后重连 ({reconnect_count}/{max_reconnects}): {e}")
                    import time
                    time.sleep(wait_time)
        
        if reconnect_count >= max_reconnects:
            logger.error("WebSocket 重连次数耗尽")
        self._running = False
    
    def _on_order_book_update(self, market_id, data: dict) -> None:
        """订单簿更新回调"""
        # 确保 market_id 是整数
        market_id = int(market_id)
        
        # 数据格式: list[{price: str, size: str}]
        raw_bids = data.get('bids', [])
        raw_asks = data.get('asks', [])
        
        # 转换为 {price: size} 格式
        bids = {b['price']: b['size'] for b in raw_bids if isinstance(b, dict)}
        asks = {a['price']: a['size'] for a in raw_asks if isinstance(a, dict)}
        
        snapshot = OrderBookSnapshot(
            market_id=market_id,
            timestamp=datetime.now(),
            bids=bids,
            asks=asks,
        )
        
        with self._lock:
            self._orderbooks[market_id] = snapshot
            logger.debug(f"订单簿更新: market={market_id}, bids={len(bids)}, asks={len(asks)}")
        
        # 通知外部回调
        if self._on_update:
            try:
                self._on_update(market_id, snapshot)
            except Exception as e:
                logger.error(f"订单簿回调错误: {e}")
    
    def get_orderbook(self, market_id: int = 0) -> Optional[OrderBookSnapshot]:
        """获取订单簿快照"""
        with self._lock:
            return self._orderbooks.get(market_id)
    
    def get_best_prices(self, market_id: int = 0) -> Optional[tuple]:
        """获取最佳买卖价 (bid, ask)"""
        ob = self.get_orderbook(market_id)
        if ob:
            bid = ob.best_bid
            ask = ob.best_ask
            if bid and ask:
                return (bid[0], ask[0])
        return None
    
    @property
    def is_running(self) -> bool:
        return self._running and self._thread and self._thread.is_alive()


# 全局实例
_ws_orderbook: Optional[WebSocketOrderBook] = None


def get_ws_orderbook() -> WebSocketOrderBook:
    """获取全局 WebSocket 订单簿实例"""
    global _ws_orderbook
    if _ws_orderbook is None:
        _ws_orderbook = WebSocketOrderBook()
    return _ws_orderbook
