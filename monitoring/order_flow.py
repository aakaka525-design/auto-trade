"""
订单流不平衡检测模块

检测买卖压力失衡，预测短期价格走势。

特性:
1. 滚动时间窗口统计
2. 买卖量比值计算
3. 连续压力信号检测

使用方法:
```python
from monitoring.order_flow import OrderFlowAnalyzer

analyzer = OrderFlowAnalyzer()

# 记录成交
analyzer.record_trade("ETH-USDT", 50000, is_buy=True)
analyzer.record_trade("ETH-USDT", 10000, is_buy=False)

# 检测不平衡
signal = analyzer.get_imbalance_signal("ETH-USDT")
if signal.is_significant:
    print(f"检测到 {signal.direction} 压力: {signal.ratio:.2f}")
```
"""
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Literal
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)


class FlowDirection(str, Enum):
    """订单流方向"""
    BUY_PRESSURE = "buy_pressure"      # 买压
    SELL_PRESSURE = "sell_pressure"    # 卖压
    BALANCED = "balanced"              # 平衡


@dataclass
class TradeRecord:
    """成交记录"""
    timestamp: datetime
    value: float
    is_buy: bool


@dataclass
class ImbalanceSignal:
    """不平衡信号"""
    symbol: str
    direction: FlowDirection
    ratio: float            # 买卖比 (>1 买压, <1 卖压)
    buy_volume: float       # 买入总量
    sell_volume: float      # 卖出总量
    trade_count: int        # 成交笔数
    window_seconds: int     # 统计窗口
    is_significant: bool    # 是否显著
    consecutive_minutes: int = 0  # 连续分钟数


@dataclass 
class SymbolFlow:
    """单个币种的订单流数据"""
    symbol: str
    trades: deque = field(default_factory=lambda: deque(maxlen=10000))
    
    # 连续信号追踪
    last_direction: Optional[FlowDirection] = None
    direction_start_time: Optional[datetime] = None


class OrderFlowAnalyzer:
    """
    订单流分析器
    
    特性:
    - 滚动时间窗口（默认 60 秒）
    - 买卖压力检测
    - 连续信号追踪
    """
    
    def __init__(
        self,
        window_seconds: int = 60,
        buy_pressure_threshold: float = 2.0,   # 买量/卖量 > 2 = 买压
        sell_pressure_threshold: float = 0.5,   # 买量/卖量 < 0.5 = 卖压
        min_trade_count: int = 10,              # 最少成交笔数
        consecutive_alert_minutes: int = 2      # 连续 N 分钟才告警
    ):
        self.window_seconds = window_seconds
        self.buy_pressure_threshold = buy_pressure_threshold
        self.sell_pressure_threshold = sell_pressure_threshold
        self.min_trade_count = min_trade_count
        self.consecutive_alert_minutes = consecutive_alert_minutes
        
        self._flows: Dict[str, SymbolFlow] = {}
    
    def _get_flow(self, symbol: str) -> SymbolFlow:
        """获取或创建币种流数据"""
        if symbol not in self._flows:
            self._flows[symbol] = SymbolFlow(symbol=symbol)
        return self._flows[symbol]
    
    def record_trade(
        self, 
        symbol: str, 
        value: float, 
        is_buy: bool,
        timestamp: datetime = None
    ) -> None:
        """
        记录成交
        
        Args:
            symbol: 交易对
            value: 成交金额
            is_buy: 是否为买入 (Taker 方向)
            timestamp: 成交时间 (默认当前时间)
        """
        flow = self._get_flow(symbol)
        flow.trades.append(TradeRecord(
            timestamp=timestamp or datetime.now(),
            value=value,
            is_buy=is_buy
        ))
    
    def _clean_old_trades(self, flow: SymbolFlow, now: datetime) -> None:
        """清理过期成交"""
        cutoff = now - timedelta(seconds=self.window_seconds * 5)  # 保留 5 个窗口
        while flow.trades and flow.trades[0].timestamp < cutoff:
            flow.trades.popleft()
    
    def get_imbalance_signal(self, symbol: str) -> ImbalanceSignal:
        """
        获取不平衡信号
        
        Returns:
            ImbalanceSignal 包含买卖压力信息
        """
        flow = self._get_flow(symbol)
        now = datetime.now()
        
        # 清理过期数据
        self._clean_old_trades(flow, now)
        
        # 统计窗口内数据
        cutoff = now - timedelta(seconds=self.window_seconds)
        recent_trades = [t for t in flow.trades if t.timestamp >= cutoff]
        
        buy_volume = sum(t.value for t in recent_trades if t.is_buy)
        sell_volume = sum(t.value for t in recent_trades if not t.is_buy)
        
        # 防止除零
        ratio = buy_volume / (sell_volume + 1e-9)
        
        # 判断方向
        if len(recent_trades) < self.min_trade_count:
            direction = FlowDirection.BALANCED
            is_significant = False
        elif ratio >= self.buy_pressure_threshold:
            direction = FlowDirection.BUY_PRESSURE
            is_significant = True
        elif ratio <= self.sell_pressure_threshold:
            direction = FlowDirection.SELL_PRESSURE
            is_significant = True
        else:
            direction = FlowDirection.BALANCED
            is_significant = False
        
        # 追踪连续时间
        consecutive_minutes = 0
        if direction != FlowDirection.BALANCED:
            if flow.last_direction == direction and flow.direction_start_time:
                consecutive_minutes = int((now - flow.direction_start_time).total_seconds() / 60)
            else:
                flow.last_direction = direction
                flow.direction_start_time = now
        else:
            flow.last_direction = None
            flow.direction_start_time = None
        
        # 只有连续足够长才显著
        if consecutive_minutes < self.consecutive_alert_minutes:
            is_significant = False
        
        return ImbalanceSignal(
            symbol=symbol,
            direction=direction,
            ratio=ratio,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            trade_count=len(recent_trades),
            window_seconds=self.window_seconds,
            is_significant=is_significant,
            consecutive_minutes=consecutive_minutes
        )
    
    def get_all_signals(self) -> List[ImbalanceSignal]:
        """获取所有币种的信号"""
        signals = []
        for symbol in self._flows.keys():
            signal = self.get_imbalance_signal(symbol)
            signals.append(signal)
        return signals
    
    def get_significant_signals(self) -> List[ImbalanceSignal]:
        """只获取显著信号"""
        return [s for s in self.get_all_signals() if s.is_significant]


# 全局实例
_order_flow_analyzer: Optional[OrderFlowAnalyzer] = None


def get_order_flow_analyzer() -> OrderFlowAnalyzer:
    """获取全局订单流分析器"""
    global _order_flow_analyzer
    if _order_flow_analyzer is None:
        _order_flow_analyzer = OrderFlowAnalyzer()
    return _order_flow_analyzer
