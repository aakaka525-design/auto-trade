"""
趋势跟踪策略

基于 EMA 交叉的趋势跟踪策略。
适用于中长时间框架 (5分钟+)。
"""
import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Deque

from connectors.base import Candlestick, OrderBook
from strategies.base import BaseStrategy, Signal, SignalAction

logger = logging.getLogger(__name__)


@dataclass
class TrendConfig:
    """趋势策略配置"""
    
    # EMA 参数
    fast_period: int = 9  # 快线
    slow_period: int = 21  # 慢线
    
    # 确认
    atr_period: int = 14  # ATR 周期
    atr_multiplier: float = 1.5  # ATR 止损倍数
    
    # 过滤
    min_trend_strength: float = 0.001  # 最小趋势强度 (EMA 差值占比)
    
    # 风控
    max_position_size: float = 1.0
    take_profit_atr: float = 2.0  # 止盈 = 2 ATR
    stop_loss_atr: float = 1.0  # 止损 = 1 ATR


class TrendFollowerStrategy(BaseStrategy):
    """
    趋势跟踪策略 (EMA 交叉)
    
    核心逻辑:
    1. 快 EMA 上穿慢 EMA => 买入
    2. 快 EMA 下穿慢 EMA => 卖出
    3. 使用 ATR 动态止盈止损
    
    使用示例:
    ```python
    strategy = TrendFollowerStrategy(
        fast_period=9,
        slow_period=21
    )
    
    for candle in candles:
        signal = strategy.on_candle(candle)
        if signal and signal.is_entry:
            await engine.submit(signal, symbol="ETH-USDC")
    ```
    """
    
    def __init__(
        self,
        fast_period: int = 9,
        slow_period: int = 21,
        **kwargs
    ):
        super().__init__("trend_follower")
        
        self.config = TrendConfig(
            fast_period=fast_period,
            slow_period=slow_period,
            **kwargs
        )
        
        # 价格历史
        self._prices: Deque[float] = deque(maxlen=200)
        self._highs: Deque[float] = deque(maxlen=200)
        self._lows: Deque[float] = deque(maxlen=200)
        
        # EMA 状态
        self._fast_ema: Optional[float] = None
        self._slow_ema: Optional[float] = None
        self._prev_fast_ema: Optional[float] = None
        self._prev_slow_ema: Optional[float] = None
        
        # 统计
        self._signal_count = 0
    
    def on_candle(self, candle: Candlestick) -> Optional[Signal]:
        """K 线回调 - 主入口"""
        if not self._enabled:
            return None
        
        # 记录数据
        self._prices.append(candle.close)
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        
        # 更新 EMA
        self._update_ema(candle.close)
        
        # 数据不足
        if self._fast_ema is None or self._slow_ema is None:
            return None
        if self._prev_fast_ema is None or self._prev_slow_ema is None:
            return None
        
        # 检测交叉
        signal = self._check_crossover(candle.close)
        
        return signal
    
    def on_orderbook(self, orderbook: OrderBook) -> Optional[Signal]:
        """订单簿回调 - 趋势策略不使用"""
        return None
    
    def _update_ema(self, price: float) -> None:
        """更新 EMA"""
        # 保存上一根的 EMA
        self._prev_fast_ema = self._fast_ema
        self._prev_slow_ema = self._slow_ema
        
        # 计算新 EMA
        if len(self._prices) < self.config.slow_period:
            return
        
        prices = list(self._prices)
        
        self._fast_ema = self._calc_ema(prices, self.config.fast_period)
        self._slow_ema = self._calc_ema(prices, self.config.slow_period)
    
    def _calc_ema(self, prices: list, period: int) -> float:
        """计算 EMA"""
        if len(prices) < period:
            return prices[-1]
        
        multiplier = 2 / (period + 1)
        ema = prices[0]
        
        for p in prices[1:]:
            ema = (p - ema) * multiplier + ema
        
        return ema
    
    def _calculate_atr(self) -> float:
        """计算 ATR"""
        if len(self._prices) < self.config.atr_period + 1:
            return 0.0
        
        highs = list(self._highs)
        lows = list(self._lows)
        closes = list(self._prices)
        
        true_ranges = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            true_ranges.append(tr)
        
        atr = sum(true_ranges[-self.config.atr_period:]) / self.config.atr_period
        return atr
    
    def _check_crossover(self, price: float) -> Optional[Signal]:
        """检测 EMA 交叉"""
        
        # 金叉: 快线从下向上穿越慢线
        golden_cross = (
            self._prev_fast_ema <= self._prev_slow_ema and
            self._fast_ema > self._slow_ema
        )
        
        # 死叉: 快线从上向下穿越慢线
        death_cross = (
            self._prev_fast_ema >= self._prev_slow_ema and
            self._fast_ema < self._slow_ema
        )
        
        if not golden_cross and not death_cross:
            return None
        
        # 计算趋势强度
        trend_strength = abs(self._fast_ema - self._slow_ema) / price
        
        if trend_strength < self.config.min_trend_strength:
            return None
        
        # 确定方向
        action = SignalAction.BUY if golden_cross else SignalAction.SELL
        
        # ATR 止盈止损
        atr = self._calculate_atr()
        if atr == 0:
            atr = price * 0.01  # 默认 1%
        
        if action == SignalAction.BUY:
            take_profit = price + atr * self.config.take_profit_atr
            stop_loss = price - atr * self.config.stop_loss_atr
        else:
            take_profit = price - atr * self.config.take_profit_atr
            stop_loss = price + atr * self.config.stop_loss_atr
        
        # 置信度 (基于趋势强度)
        confidence = min(trend_strength / 0.01, 1.0)
        
        self._signal_count += 1
        
        cross_type = "金叉" if golden_cross else "死叉"
        logger.info(
            f"[Trend] {cross_type} #{self._signal_count}: "
            f"{action.value} @ ${price:.2f} "
            f"(Fast={self._fast_ema:.2f}, Slow={self._slow_ema:.2f})"
        )
        
        return self._emit_signal(Signal(
            action=action,
            confidence=confidence,
            price=price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            reason=f"EMA {cross_type}: Fast={self._fast_ema:.2f}, Slow={self._slow_ema:.2f}",
            metadata={
                "fast_ema": self._fast_ema,
                "slow_ema": self._slow_ema,
                "atr": atr,
                "trend_strength": trend_strength,
            }
        ))
    
    def get_stats(self) -> dict:
        """获取策略统计"""
        return {
            "name": self.name,
            "enabled": self._enabled,
            "signal_count": self._signal_count,
            "fast_ema": self._fast_ema,
            "slow_ema": self._slow_ema,
            "config": {
                "fast_period": self.config.fast_period,
                "slow_period": self.config.slow_period,
            }
        }
