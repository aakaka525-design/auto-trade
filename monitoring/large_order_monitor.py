"""
大单监控警报系统

监控订单簿中的大单挂单，达到阈值时发出警报。
"""
import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional, List
from dataclasses import dataclass, field

from connectors.base import OrderBook, OrderBookLevel

logger = logging.getLogger(__name__)


@dataclass
class LargeOrder:
    """大单信息"""
    side: str  # "bid" or "ask"
    price: float
    size: float
    value_usdc: float  # 价值 USDC
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __str__(self):
        return f"[{self.side.upper()}] ${self.price:,.2f} x {self.size:.4f} (${self.value_usdc:,.0f})"


@dataclass
class AlertConfig:
    """警报配置"""
    min_size: float = 10.0  # 最小数量 (ETH)
    min_value_usdc: float = 30000.0  # 最小价值 ($)
    price_range_pct: float = 1.0  # 距离中间价的百分比范围
    cooldown_seconds: float = 60.0  # 同价格警报冷却时间


class LargeOrderMonitor:
    """
    大单监控器
    
    实时监控订单簿，检测大单挂单并发出警报。
    
    使用示例:
    ```python
    monitor = LargeOrderMonitor(
        min_size=10.0,       # 最小 10 ETH
        min_value_usdc=30000,  # 或价值 3 万美元
        on_alert=lambda order: print(f"🚨 大单警报: {order}")
    )
    
    # 在交易循环中调用
    orderbook = await connector.get_orderbook("ETH-USDC")
    monitor.check(orderbook)
    ```
    """
    
    def __init__(
        self,
        min_size: float = 10.0,
        min_value_usdc: float = 30000.0,
        price_range_pct: float = 1.0,
        cooldown_seconds: float = 60.0,
        on_alert: Optional[Callable[[LargeOrder], None]] = None,
    ):
        self.config = AlertConfig(
            min_size=min_size,
            min_value_usdc=min_value_usdc,
            price_range_pct=price_range_pct,
            cooldown_seconds=cooldown_seconds,
        )
        self._on_alert = on_alert
        
        # 已警报的价格 -> 时间戳
        self._alerted: dict[float, datetime] = {}
        
        # 统计
        self._total_alerts = 0
        self._large_orders_history: List[LargeOrder] = []
    
    def check(self, orderbook: OrderBook) -> List[LargeOrder]:
        """
        检查订单簿中的大单
        
        Returns:
            检测到的大单列表
        """
        large_orders = []
        now = datetime.now()
        
        # 计算中间价
        if not orderbook.bids or not orderbook.asks:
            return []
        
        best_bid = orderbook.bids[0].price
        best_ask = orderbook.asks[0].price
        mid_price = (best_bid + best_ask) / 2
        
        # 价格范围
        price_range = mid_price * (self.config.price_range_pct / 100)
        min_price = mid_price - price_range
        max_price = mid_price + price_range
        
        # 检查买单
        for level in orderbook.bids:
            if level.price < min_price:
                continue  # 超出范围
            
            large_order = self._check_level("bid", level, mid_price, now)
            if large_order:
                large_orders.append(large_order)
        
        # 检查卖单
        for level in orderbook.asks:
            if level.price > max_price:
                continue  # 超出范围
            
            large_order = self._check_level("ask", level, mid_price, now)
            if large_order:
                large_orders.append(large_order)
        
        # 触发警报
        for order in large_orders:
            self._trigger_alert(order)
        
        return large_orders
    
    def _check_level(
        self, 
        side: str, 
        level: OrderBookLevel, 
        mid_price: float,
        now: datetime
    ) -> Optional[LargeOrder]:
        """检查单个价格档位"""
        value_usdc = level.price * level.size
        
        # 检查是否满足阈值
        is_large = (
            level.size >= self.config.min_size or 
            value_usdc >= self.config.min_value_usdc
        )
        
        if not is_large:
            return None
        
        # 检查冷却
        if level.price in self._alerted:
            elapsed = (now - self._alerted[level.price]).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                return None  # 还在冷却中
        
        return LargeOrder(
            side=side,
            price=level.price,
            size=level.size,
            value_usdc=value_usdc,
            timestamp=now,
        )
    
    def _trigger_alert(self, order: LargeOrder) -> None:
        """触发警报"""
        self._alerted[order.price] = order.timestamp
        self._total_alerts += 1
        self._large_orders_history.append(order)
        
        # 保留最近 100 条
        if len(self._large_orders_history) > 100:
            self._large_orders_history = self._large_orders_history[-100:]
        
        logger.warning(f"🚨 大单警报: {order}")
        
        if self._on_alert:
            try:
                self._on_alert(order)
            except Exception as e:
                logger.error(f"警报回调错误: {e}")
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            "total_alerts": self._total_alerts,
            "recent_orders": len(self._large_orders_history),
            "config": {
                "min_size": self.config.min_size,
                "min_value_usdc": self.config.min_value_usdc,
                "price_range_pct": self.config.price_range_pct,
            }
        }
    
    def get_recent_orders(self, limit: int = 10) -> List[LargeOrder]:
        """获取最近的大单"""
        return self._large_orders_history[-limit:]


# 便捷函数
def find_large_orders(
    orderbook: OrderBook, 
    min_size: float = 10.0,
    min_value: float = 30000.0
) -> List[LargeOrder]:
    """
    快速查找订单簿中的大单
    
    Args:
        orderbook: 订单簿
        min_size: 最小数量
        min_value: 最小价值 USDC
    
    Returns:
        大单列表
    """
    large_orders = []
    
    for level in orderbook.bids:
        value = level.price * level.size
        if level.size >= min_size or value >= min_value:
            large_orders.append(LargeOrder(
                side="bid",
                price=level.price,
                size=level.size,
                value_usdc=value,
            ))
    
    for level in orderbook.asks:
        value = level.price * level.size
        if level.size >= min_size or value >= min_value:
            large_orders.append(LargeOrder(
                side="ask",
                price=level.price,
                size=level.size,
                value_usdc=value,
            ))
    
    # 按价值排序
    large_orders.sort(key=lambda x: x.value_usdc, reverse=True)
    return large_orders
