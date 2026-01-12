"""
大单跟踪模块 v2.0 (Whale Tracking)

识别机构行为模式，包含动态阈值和 Stop Hunt 检测。

特性:
1. 动态阈值 (EMA 24h Vol * 0.01)
2. Price Wall Persistence (价格墙持久性)
3. Stop Hunt 检测 (击穿 + 反弹 + 成交量飙升)
4. EMA 实时更新

使用方法:
```python
from monitoring.whale_tracker import get_whale_tracker

tracker = get_whale_tracker()
tracker.update_volume("ETH-USDT", 1000000)  # 更新成交量
tracker.record_large_order("ETH-USDT", "buy", 500000, 2.0)
tracker.update_price("ETH-USDT", 3500.0, 100000)  # 价格 + 成交量

patterns = tracker.detect_patterns("ETH-USDT")
stop_hunt = tracker.detect_stop_hunt("ETH-USDT")
```
"""
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Literal
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)


class PatternType(str, Enum):
    """行为模式类型"""
    ACCUMULATION = "accumulation"      # 连续买入 (建仓)
    DISTRIBUTION = "distribution"      # 连续卖出 (出货)
    PRICE_WALL = "price_wall"          # 价格墙持久
    STOP_HUNT = "stop_hunt"            # 猎杀止损


@dataclass
class LargeOrderRecord:
    """大单记录"""
    timestamp: datetime
    symbol: str
    side: Literal["buy", "sell"]
    value: float
    slippage: float


@dataclass
class PriceRecord:
    """价格记录"""
    timestamp: datetime
    price: float
    volume: float


@dataclass
class WhalePattern:
    """鲸鱼行为模式"""
    pattern_type: PatternType
    symbol: str
    description: str
    order_count: int = 0
    total_value: float = 0.0
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class StopHuntSignal:
    """猎杀止损信号"""
    symbol: str
    is_detected: bool
    support_price: float
    breakthrough_price: float
    rebound_price: float
    volume_spike_ratio: float
    description: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class SymbolTracker:
    """单个币种的跟踪数据"""
    symbol: str
    
    # 成交量 EMA
    volume_ema: float = 0.0
    volume_ema_alpha: float = 0.1  # EMA 平滑系数
    last_volume_update: Optional[datetime] = None
    
    # 大单记录
    orders: deque = field(default_factory=lambda: deque(maxlen=100))
    
    # 价格历史 (用于 Stop Hunt)
    price_history: deque = field(default_factory=lambda: deque(maxlen=3600))  # 1 小时
    
    # Price Wall 跟踪
    price_walls: Dict[float, Tuple[float, datetime]] = field(default_factory=dict)  # {price: (size, first_seen)}
    
    # 动态阈值
    dynamic_threshold: float = 50000.0  # 默认 50K


class WhaleTracker:
    """
    鲸鱼追踪器 v2.0
    
    特性:
    - 动态阈值: Trade_Val > EMA(24h_Vol) * threshold_ratio
    - Price Wall Persistence
    - Stop Hunt 检测
    """
    
    def __init__(
        self,
        window_minutes: int = 30,
        min_orders_for_pattern: int = 3,
        accumulation_ratio: float = 0.8,
        threshold_ratio: float = 0.01,       # 大单 = 1% 的 24h Vol
        wall_persist_minutes: float = 5.0,   # 价格墙持久阈值
        stop_hunt_rebound_seconds: float = 10.0,
        stop_hunt_volume_ratio: float = 3.0,  # 成交量需要是平均的 3 倍
    ):
        self.window_minutes = window_minutes
        self.min_orders_for_pattern = min_orders_for_pattern
        self.accumulation_ratio = accumulation_ratio
        self.threshold_ratio = threshold_ratio
        self.wall_persist_minutes = wall_persist_minutes
        self.stop_hunt_rebound_seconds = stop_hunt_rebound_seconds
        self.stop_hunt_volume_ratio = stop_hunt_volume_ratio
        
        self._trackers: Dict[str, SymbolTracker] = {}
        self._all_orders: deque = deque(maxlen=1000)
    
    def _get_tracker(self, symbol: str) -> SymbolTracker:
        """获取或创建币种跟踪器"""
        if symbol not in self._trackers:
            self._trackers[symbol] = SymbolTracker(symbol=symbol)
        return self._trackers[symbol]
    
    def update_volume(self, symbol: str, volume_24h: float) -> None:
        """
        更新 24h 成交量 (用于动态阈值)
        
        使用 EMA 平滑，每 5 分钟调用一次
        """
        tracker = self._get_tracker(symbol)
        now = datetime.now()
        
        if tracker.volume_ema <= 0:
            tracker.volume_ema = volume_24h
        else:
            tracker.volume_ema = (
                tracker.volume_ema_alpha * volume_24h + 
                (1 - tracker.volume_ema_alpha) * tracker.volume_ema
            )
        
        # 更新动态阈值
        tracker.dynamic_threshold = max(
            tracker.volume_ema * self.threshold_ratio,
            10000.0  # 最低 10K
        )
        tracker.last_volume_update = now
    
    def get_dynamic_threshold(self, symbol: str) -> float:
        """获取动态阈值"""
        tracker = self._get_tracker(symbol)
        return tracker.dynamic_threshold
    
    def is_large_order(self, symbol: str, value: float) -> bool:
        """判断是否为大单"""
        return value >= self.get_dynamic_threshold(symbol)
    
    def record_large_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        value: float,
        slippage: float,
        timestamp: datetime = None
    ) -> bool:
        """
        记录大单 (如果超过动态阈值)
        
        Returns:
            是否被记录为大单
        """
        if not self.is_large_order(symbol, value):
            return False
        
        record = LargeOrderRecord(
            timestamp=timestamp or datetime.now(),
            symbol=symbol,
            side=side,
            value=value,
            slippage=slippage
        )
        
        tracker = self._get_tracker(symbol)
        tracker.orders.append(record)
        self._all_orders.append(record)
        
        logger.debug(
            f"🐋 大单记录: {symbol} {side} ${value:,.0f} "
            f"(阈值 ${tracker.dynamic_threshold:,.0f})"
        )
        return True
    
    def update_price(
        self, 
        symbol: str, 
        price: float, 
        volume: float = 0,
        timestamp: datetime = None
    ) -> None:
        """更新价格和成交量 (用于 Stop Hunt)"""
        tracker = self._get_tracker(symbol)
        tracker.price_history.append(PriceRecord(
            timestamp=timestamp or datetime.now(),
            price=price,
            volume=volume
        ))
    
    def update_price_wall(
        self, 
        symbol: str, 
        price: float, 
        size: float
    ) -> None:
        """
        更新价格墙
        
        Args:
            price: 价格档位
            size: 当前挂单量 (0 表示撤销)
        """
        tracker = self._get_tracker(symbol)
        now = datetime.now()
        
        price_key = round(price, 4)  # 归一化价格
        
        if size > 0:
            if price_key in tracker.price_walls:
                old_size, first_seen = tracker.price_walls[price_key]
                # 更新大小，保持首次发现时间
                tracker.price_walls[price_key] = (size, first_seen)
            else:
                tracker.price_walls[price_key] = (size, now)
        else:
            # 撤销
            tracker.price_walls.pop(price_key, None)
    
    def detect_price_wall_patterns(self, symbol: str) -> List[WhalePattern]:
        """检测持久价格墙"""
        tracker = self._get_tracker(symbol)
        now = datetime.now()
        patterns = []
        
        persist_threshold = timedelta(minutes=self.wall_persist_minutes)
        
        for price, (size, first_seen) in tracker.price_walls.items():
            duration = now - first_seen
            if duration >= persist_threshold:
                value = price * size
                if self.is_large_order(symbol, value):
                    patterns.append(WhalePattern(
                        pattern_type=PatternType.PRICE_WALL,
                        symbol=symbol,
                        description=f"价格 {price:.2f} 处挂单 ${value:,.0f} 持续 {duration.seconds // 60} 分钟",
                        total_value=value,
                        confidence=min(1.0, duration.seconds / 600)  # 10 分钟满分
                    ))
        
        return patterns
    
    def detect_stop_hunt(self, symbol: str) -> StopHuntSignal:
        """
        检测 Stop Hunt (猎杀止损)
        
        逻辑:
        1. 价格击穿 1h 最低点
        2. 10秒内反弹回最低点之上
        3. 反弹期间成交量飙升
        """
        tracker = self._get_tracker(symbol)
        now = datetime.now()
        
        # 空信号
        null_signal = StopHuntSignal(
            symbol=symbol,
            is_detected=False,
            support_price=0,
            breakthrough_price=0,
            rebound_price=0,
            volume_spike_ratio=0,
            description=""
        )
        
        if len(tracker.price_history) < 100:
            return null_signal
        
        # 获取 1h 价格历史
        cutoff_1h = now - timedelta(hours=1)
        recent_prices = [p for p in tracker.price_history if p.timestamp >= cutoff_1h]
        
        if len(recent_prices) < 10:
            return null_signal
        
        # 计算支撑位 (1h 最低)
        prices = [p.price for p in recent_prices]
        support_price = min(prices)
        
        # 检查最近 10 秒
        cutoff_recent = now - timedelta(seconds=self.stop_hunt_rebound_seconds)
        very_recent = [p for p in recent_prices if p.timestamp >= cutoff_recent]
        
        if len(very_recent) < 3:
            return null_signal
        
        # 检查击穿
        breakthrough = [p for p in very_recent if p.price < support_price]
        if not breakthrough:
            return null_signal
        
        breakthrough_price = min(p.price for p in breakthrough)
        
        # 检查反弹
        rebound = [p for p in very_recent if p.price >= support_price and p.timestamp > breakthrough[0].timestamp]
        if not rebound:
            return null_signal
        
        rebound_price = rebound[-1].price
        
        # 检查成交量飙升
        avg_volume = sum(p.volume for p in recent_prices) / len(recent_prices) if recent_prices else 0
        recent_volume = sum(p.volume for p in very_recent)
        volume_ratio = recent_volume / (avg_volume * len(very_recent) + 1e-9)
        
        if volume_ratio < self.stop_hunt_volume_ratio:
            return null_signal
        
        return StopHuntSignal(
            symbol=symbol,
            is_detected=True,
            support_price=support_price,
            breakthrough_price=breakthrough_price,
            rebound_price=rebound_price,
            volume_spike_ratio=volume_ratio,
            description=f"击穿 {support_price:.2f} → {breakthrough_price:.2f}，10秒内反弹至 {rebound_price:.2f}，成交量 {volume_ratio:.1f}x"
        )
    
    def detect_patterns(self, symbol: str) -> List[WhalePattern]:
        """检测所有模式"""
        tracker = self._get_tracker(symbol)
        cutoff = datetime.now() - timedelta(minutes=self.window_minutes)
        orders = [o for o in tracker.orders if o.timestamp >= cutoff]
        
        if len(orders) < self.min_orders_for_pattern:
            return []
        
        patterns = []
        
        # 统计
        buy_orders = [o for o in orders if o.side == "buy"]
        sell_orders = [o for o in orders if o.side == "sell"]
        total = len(orders)
        
        buy_ratio = len(buy_orders) / total
        sell_ratio = len(sell_orders) / total
        
        # 建仓
        if buy_ratio >= self.accumulation_ratio:
            patterns.append(WhalePattern(
                pattern_type=PatternType.ACCUMULATION,
                symbol=symbol,
                description=f"连续 {len(buy_orders)} 笔买入大单",
                order_count=len(buy_orders),
                total_value=sum(o.value for o in buy_orders),
                confidence=buy_ratio
            ))
        
        # 出货
        elif sell_ratio >= self.accumulation_ratio:
            patterns.append(WhalePattern(
                pattern_type=PatternType.DISTRIBUTION,
                symbol=symbol,
                description=f"连续 {len(sell_orders)} 笔卖出大单",
                order_count=len(sell_orders),
                total_value=sum(o.value for o in sell_orders),
                confidence=sell_ratio
            ))
        
        # 价格墙
        patterns.extend(self.detect_price_wall_patterns(symbol))
        
        return patterns
    
    def get_all_patterns(self) -> List[WhalePattern]:
        """获取所有币种的模式"""
        patterns = []
        for symbol in self._trackers.keys():
            patterns.extend(self.detect_patterns(symbol))
        return patterns


# 全局实例
_whale_tracker: Optional[WhaleTracker] = None


def get_whale_tracker() -> WhaleTracker:
    """获取全局鲸鱼追踪器"""
    global _whale_tracker
    if _whale_tracker is None:
        _whale_tracker = WhaleTracker()
    return _whale_tracker
