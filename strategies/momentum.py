"""
动量策略

基于价格变化率(ROC)和成交量的动量策略。
适用于秒级/分钟级时间框架。
"""
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Deque, List

from connectors.base import Candlestick, OrderBook
from strategies.base import BaseStrategy, Signal, SignalAction

logger = logging.getLogger(__name__)


@dataclass
class MomentumConfig:
    """动量策略配置"""
    
    # 动量参数
    roc_period: int = 10  # ROC 周期
    roc_threshold: float = 0.002  # 0.2% 触发阈值
    
    # 确认指标
    volume_ma_period: int = 20  # 成交量均线周期
    volume_ratio_threshold: float = 1.5  # 放量确认阈值
    
    # 风控
    max_position_size: float = 1.0
    take_profit_pct: float = 0.005  # 0.5%
    stop_loss_pct: float = 0.003  # 0.3%
    
    # 冷却
    min_signal_interval_sec: float = 5.0


class MomentumStrategy(BaseStrategy):
    """
    动量策略
    
    核心逻辑:
    1. 计算价格变化率 (ROC) = (当前价 - N周期前价) / N周期前价
    2. ROC > 阈值 且 放量 => 买入信号
    3. ROC < -阈值 且 放量 => 卖出信号
    
    使用示例:
    ```python
    strategy = MomentumStrategy(
        roc_period=10,
        roc_threshold=0.002
    )
    
    # 接收 K 线
    for candle in candles:
        signal = strategy.on_candle(candle)
        if signal and signal.is_entry:
            await engine.submit(signal, symbol="ETH-USDC")
    ```
    """
    
    def __init__(
        self,
        roc_period: int = 10,
        roc_threshold: float = 0.002,
        **kwargs
    ):
        super().__init__("momentum")
        
        self.config = MomentumConfig(
            roc_period=roc_period,
            roc_threshold=roc_threshold,
            **kwargs
        )
        
        # 价格/成交量历史
        self._prices: Deque[float] = deque(maxlen=100)
        self._volumes: Deque[float] = deque(maxlen=100)
        
        # 状态
        self._last_signal_time: Optional[datetime] = None
        self._current_position: float = 0.0
        
        # 统计
        self._signal_count = 0
    
    def on_candle(self, candle: Candlestick) -> Optional[Signal]:
        """K 线回调 - 主入口"""
        if not self._enabled:
            return None
        
        # 记录数据
        self._prices.append(candle.close)
        self._volumes.append(candle.volume)
        
        # 数据不足
        if len(self._prices) < self.config.roc_period + 1:
            return None
        
        # 检查冷却
        if not self._check_cooldown():
            return None
        
        # 计算指标
        roc = self._calculate_roc()
        volume_ratio = self._calculate_volume_ratio()
        
        # 评估信号
        return self._evaluate_signal(
            roc=roc,
            volume_ratio=volume_ratio,
            price=candle.close
        )
    
    def on_orderbook(self, orderbook: OrderBook) -> Optional[Signal]:
        """订单簿回调 - 动量策略不使用"""
        return None
    
    def _calculate_roc(self) -> float:
        """
        计算价格变化率 (Rate of Change)
        
        ROC = (当前价 - N周期前价) / N周期前价
        """
        if len(self._prices) <= self.config.roc_period:
            return 0.0
        
        current = self._prices[-1]
        previous = self._prices[-self.config.roc_period - 1]
        
        if previous == 0:
            return 0.0
        
        return (current - previous) / previous
    
    def _calculate_volume_ratio(self) -> float:
        """计算成交量相对于均值的比率"""
        if len(self._volumes) < self.config.volume_ma_period:
            return 1.0
        
        ma = sum(list(self._volumes)[-self.config.volume_ma_period:]) / self.config.volume_ma_period
        current = self._volumes[-1]
        
        if ma == 0:
            return 1.0
        
        return current / ma
    
    def _evaluate_signal(
        self,
        roc: float,
        volume_ratio: float,
        price: float
    ) -> Optional[Signal]:
        """评估是否产生信号"""
        
        # 动量不足
        if abs(roc) < self.config.roc_threshold:
            return None
        
        # 成交量确认 (可选)
        volume_confirmed = volume_ratio >= self.config.volume_ratio_threshold
        
        # 确定方向
        if roc > self.config.roc_threshold:
            action = SignalAction.BUY
        elif roc < -self.config.roc_threshold:
            action = SignalAction.SELL
        else:
            return None
        
        # 计算置信度
        # 基础: ROC 强度 + 成交量加成
        base_confidence = min(abs(roc) / (self.config.roc_threshold * 5), 0.8)
        volume_bonus = 0.2 if volume_confirmed else 0.0
        confidence = min(base_confidence + volume_bonus, 1.0)
        
        # 计算止盈止损
        if action == SignalAction.BUY:
            take_profit = price * (1 + self.config.take_profit_pct)
            stop_loss = price * (1 - self.config.stop_loss_pct)
        else:
            take_profit = price * (1 - self.config.take_profit_pct)
            stop_loss = price * (1 + self.config.stop_loss_pct)
        
        self._signal_count += 1
        self._last_signal_time = datetime.now()
        
        logger.debug(
            f"[Momentum] 信号 #{self._signal_count}: "
            f"{action.value} @ ${price:.2f} "
            f"(ROC={roc:.4%}, Vol Ratio={volume_ratio:.2f})"
        )
        
        return self._emit_signal(Signal(
            action=action,
            confidence=confidence,
            price=price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            reason=f"ROC: {roc:.4%}, Volume: {volume_ratio:.2f}x",
            metadata={
                "roc": roc,
                "volume_ratio": volume_ratio,
                "volume_confirmed": volume_confirmed,
            }
        ))
    
    def _check_cooldown(self) -> bool:
        """检查冷却时间"""
        if self._last_signal_time is None:
            return True
        
        elapsed = (datetime.now() - self._last_signal_time).total_seconds()
        return elapsed >= self.config.min_signal_interval_sec
    
    def get_stats(self) -> dict:
        """获取策略统计"""
        return {
            "name": self.name,
            "enabled": self._enabled,
            "signal_count": self._signal_count,
            "data_points": len(self._prices),
            "config": {
                "roc_period": self.config.roc_period,
                "roc_threshold": self.config.roc_threshold,
            }
        }
