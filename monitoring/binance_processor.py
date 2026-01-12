"""
Binance 数据处理模块

处理成交、深度数据，集成智能算法。
"""
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from enum import Enum

from config import settings

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    """告警级别"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class BinanceProcessor:
    """
    Binance 数据处理器
    
    功能:
    - 成交数据处理 (VWAP 滑点)
    - 深度数据处理
    - 智能算法集成
    - 告警发送
    """
    
    def __init__(self, notifier=None):
        """
        Args:
            notifier: Telegram 通知器
        """
        # 滑点阈值
        self.slippage_low = getattr(settings, 'SLIPPAGE_THRESHOLD_LOW', 0.5)
        self.slippage_medium = getattr(settings, 'SLIPPAGE_THRESHOLD_MED', 2.0)
        self.slippage_high = getattr(settings, 'SLIPPAGE_THRESHOLD_HIGH', 10.0)
        
        # 最低金额阈值
        self.min_order_spot = getattr(settings, 'MIN_ORDER_VALUE_SPOT', 50000.0)
        self.min_order_futures = getattr(settings, 'MIN_ORDER_VALUE_FUTURES', 20000.0)
        self.orderbook_depth = getattr(settings, 'ORDERBOOK_DEPTH', 50)
        self.skip_top_levels = getattr(settings, 'SKIP_TOP_LEVELS', 1)
        
        # 订单簿缓存
        self.orderbook_bids: Dict[str, List[tuple]] = {}
        self.orderbook_asks: Dict[str, List[tuple]] = {}
        self.price_cache: Dict[str, float] = {}
        
        # 冷却控制
        self.alert_cooldown: Dict[str, datetime] = {}
        self.cooldown_seconds = getattr(settings, 'PRICE_COOLDOWN', 120)
        
        # 统计
        self.stats = {
            "trades": 0,
            "depth_updates": 0,
            "alerts_low": 0,
            "alerts_medium": 0,
            "alerts_high": 0,
        }
        
        # 通知器
        self.notifier = notifier
        
        # 智能算法 (延迟加载)
        self._smart_filter = None
        self._book_imbalance = None
        self._whale_tracker = None
        self._basis_tracker = None
    
    @property
    def smart_filter(self):
        if self._smart_filter is None:
            from monitoring.smart_filter import get_smart_filter
            self._smart_filter = get_smart_filter()
        return self._smart_filter
    
    @property
    def book_imbalance(self):
        if self._book_imbalance is None:
            from monitoring.book_imbalance import get_book_imbalance_analyzer
            self._book_imbalance = get_book_imbalance_analyzer()
        return self._book_imbalance
    
    @property
    def whale_tracker(self):
        if self._whale_tracker is None:
            from monitoring.whale_tracker import get_whale_tracker
            self._whale_tracker = get_whale_tracker()
        return self._whale_tracker
    
    @property
    def basis_tracker(self):
        if self._basis_tracker is None:
            from monitoring.basis_tracker import get_basis_tracker
            self._basis_tracker = get_basis_tracker()
        return self._basis_tracker
    
    def get_alert_level(self, slippage: float) -> Optional[str]:
        """根据滑点返回告警级别"""
        if slippage >= self.slippage_high:
            return AlertLevel.HIGH
        elif slippage >= self.slippage_medium:
            return AlertLevel.MEDIUM
        elif slippage >= self.slippage_low:
            return AlertLevel.LOW
        return None
    
    def get_min_order(self, market: str) -> float:
        """获取最低金额阈值"""
        return self.min_order_spot if market == "spot" else self.min_order_futures
    
    def is_in_cooldown(self, key: str) -> bool:
        """检查是否在冷却期"""
        if key not in self.alert_cooldown:
            return False
        elapsed = (datetime.now() - self.alert_cooldown[key]).total_seconds()
        return elapsed < self.cooldown_seconds
    
    def set_cooldown(self, key: str):
        """设置冷却"""
        self.alert_cooldown[key] = datetime.now()
    
    def calculate_slippage(
        self, 
        cache_key: str, 
        order_value: float, 
        is_buy: bool
    ) -> Tuple[float, float]:
        """
        模拟成交计算滑点
        
        Returns:
            (滑点%, VWAP价格)
        """
        if is_buy:
            orderbook = self.orderbook_asks.get(cache_key, [])
        else:
            orderbook = self.orderbook_bids.get(cache_key, [])
        
        if not orderbook:
            return 0, 0
        
        min_levels = 10
        skip = self.skip_top_levels
        if len(orderbook) < min_levels + skip:
            return 0, 0
        
        current_price = orderbook[skip][0]
        
        remaining = order_value
        total_qty = 0
        total_value = 0
        
        for price, qty in orderbook[skip:]:
            level_value = price * qty
            if level_value >= remaining:
                use_qty = remaining / price
                total_qty += use_qty
                total_value += remaining
                break
            else:
                total_qty += qty
                total_value += level_value
                remaining -= level_value
        
        if total_qty <= 0:
            return 0, 0
        
        vwap = total_value / total_qty
        slippage = abs(vwap - current_price) / current_price * 100
        
        return slippage, vwap
    
    def update_orderbook(self, cache_key: str, bids: List, asks: List):
        """更新订单簿 (全量替换)"""
        if bids:
            self.orderbook_bids[cache_key] = [
                (float(p), float(q)) for p, q in bids if float(q) > 0
            ][:self.orderbook_depth]
        
        if asks:
            self.orderbook_asks[cache_key] = [
                (float(p), float(q)) for p, q in asks if float(q) > 0
            ][:self.orderbook_depth]
    
    async def process_trade(
        self, 
        symbol: str, 
        price: float, 
        size: float, 
        is_buyer_maker: bool, 
        market: str = "spot"
    ):
        """处理成交数据"""
        self.stats["trades"] += 1
        
        value = price * size
        is_buy = not is_buyer_maker
        side = "BUY" if is_buy else "SELL"
        
        cache_key = f"{market}:{symbol}"
        self.price_cache[cache_key] = price
        
        # 智能算法: 更新价格历史
        self.whale_tracker.update_price(symbol, price, value)
        
        # 检查最低金额
        min_order = self.get_min_order(market)
        if value < min_order:
            return
        
        # 计算滑点
        slippage, vwap = self.calculate_slippage(cache_key, value, is_buy)
        
        # 智能算法: 记录滑点
        self.smart_filter.record_slippage(symbol, slippage)
        
        # 智能算法: 智能过滤
        should_alert, _ = self.smart_filter.should_alert(symbol, slippage, value)
        if not should_alert:
            return
        
        level = self.get_alert_level(slippage)
        if not level:
            return
        
        key = f"{market}:{symbol}:trade:{int(price)}"
        if self.is_in_cooldown(key):
            return
        
        # 智能算法: 鲸鱼追踪
        self.whale_tracker.record_large_order(
            symbol=symbol,
            side="buy" if is_buy else "sell",
            value=value,
            slippage=slippage
        )
        
        # 更新统计
        if level == AlertLevel.HIGH:
            self.stats["alerts_high"] += 1
        elif level == AlertLevel.MEDIUM:
            self.stats["alerts_medium"] += 1
        else:
            self.stats["alerts_low"] += 1
        
        await self._send_trade_alert(symbol, side, price, size, value, slippage, market, level)
        self.set_cooldown(key)
    
    async def process_depth(
        self, 
        symbol: str, 
        bids: List, 
        asks: List, 
        market: str = "spot"
    ):
        """处理深度数据"""
        self.stats["depth_updates"] += 1
        
        cache_key = f"{market}:{symbol}"
        self.update_orderbook(cache_key, bids, asks)
        
        # 智能算法: 深度不平衡 (WBI-Lite v3.x)
        bid_levels = [(float(p), float(q)) for p, q in bids[:10] if float(q) > 0]
        ask_levels = [(float(p), float(q)) for p, q in asks[:10] if float(q) > 0]
        
        self.book_imbalance.get_signal(cache_key, bid_levels, ask_levels)
        
        # 基差追踪器: 更新价格
        if bid_levels and ask_levels:
            best_bid = bid_levels[0][0]
            best_ask = ask_levels[0][0]
            mid_price = (best_bid + best_ask) / 2
            is_futures = (market == "futures")
            self.basis_tracker.update_price(symbol, mid_price, is_futures)
    
    def get_pending_wbi_signals(self) -> List:
        """获取并清空待处理的 WBI 信号"""
        return self.book_imbalance.get_pending_signals()
    
    def get_pending_basis_alerts(self) -> List:
        """获取并清空待处理的基差警报"""
        return self.basis_tracker.get_pending_alerts()
    
    async def _send_trade_alert(
        self, 
        symbol: str, 
        side: str, 
        price: float, 
        size: float, 
        value: float, 
        slippage: float, 
        market: str,
        level: str
    ):
        """发送成交告警"""
        from connectors.binance.auth import SymbolConverter
        
        readable = SymbolConverter.to_readable(symbol, "USDT")
        market_emoji = "📈" if market == "futures" else "💰"
        level_emoji = "🔴" if level == AlertLevel.HIGH else "🟡" if level == AlertLevel.MEDIUM else "🟢"
        
        message = (
            f"{level_emoji} {market_emoji} *{readable}* ({market.upper()})\n"
            f"方向: {side}\n"
            f"价格: ${price:,.4f}\n"
            f"数量: {size:,.4f}\n"
            f"金额: ${value:,.0f}\n"
            f"滑点: {slippage:.2f}%"
        )
        
        logger.info(f"{level_emoji} {readable} | {side} | ${value:,.0f} | 滑点 {slippage:.2f}%")
        
        if self.notifier:
            await self.notifier.send(message, level)
