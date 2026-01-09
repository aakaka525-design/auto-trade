"""
策略抽象基类

所有交易策略必须继承 BaseStrategy 并实现信号生成逻辑。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any
from enum import Enum

from connectors.base import Candlestick, OrderBook, Trade, OrderSide


# ==================== 信号定义 ====================

class SignalAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass
class Signal:
    """交易信号"""
    action: SignalAction
    confidence: float  # 0.0 - 1.0
    price: float  # 信号生成时的价格
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    reason: str = ""
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "strategy"
    
    def to_global_id(self) -> str:
        """生成 Global Signal ID"""
        ts = int(self.timestamp.timestamp())
        return f"SIG_{self.action.value.upper()}_{ts}"
    
    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.BUY, SignalAction.SELL)
    
    @property
    def side(self) -> Optional[OrderSide]:
        if self.action == SignalAction.BUY:
            return OrderSide.BUY
        elif self.action == SignalAction.SELL:
            return OrderSide.SELL
        return None


# ==================== 策略基类 ====================

class BaseStrategy(ABC):
    """
    策略抽象基类
    
    生命周期:
    1. __init__: 初始化策略参数
    2. on_start: 策略启动时调用
    3. on_tick/on_candle/on_orderbook: 接收市场数据
    4. generate_signal: 生成交易信号
    5. on_stop: 策略停止时调用
    
    使用示例:
    ```python
    class MomentumStrategy(BaseStrategy):
        def __init__(self, lookback: int = 20):
            super().__init__("momentum")
            self.lookback = lookback
            self.prices = []
        
        def on_candle(self, candle: Candlestick) -> Optional[Signal]:
            self.prices.append(candle.close)
            if len(self.prices) < self.lookback:
                return None
            
            momentum = self.prices[-1] / self.prices[-self.lookback] - 1
            
            if momentum > 0.02:
                return Signal(
                    action=SignalAction.BUY,
                    confidence=min(momentum * 10, 1.0),
                    price=candle.close,
                    reason=f"Momentum +{momentum:.2%}"
                )
            elif momentum < -0.02:
                return Signal(
                    action=SignalAction.SELL,
                    confidence=min(abs(momentum) * 10, 1.0),
                    price=candle.close,
                    reason=f"Momentum {momentum:.2%}"
                )
            return Signal(action=SignalAction.HOLD, confidence=0.5, price=candle.close)
    ```
    """
    
    def __init__(self, name: str):
        self.name = name
        self._enabled = True
        self._last_signal: Optional[Signal] = None
    
    @property
    def is_enabled(self) -> bool:
        return self._enabled
    
    @property
    def last_signal(self) -> Optional[Signal]:
        return self._last_signal
    
    def enable(self) -> None:
        self._enabled = True
    
    def disable(self) -> None:
        self._enabled = False
    
    # ==================== 生命周期 ====================
    
    async def on_start(self) -> None:
        """策略启动时调用"""
        pass
    
    async def on_stop(self) -> None:
        """策略停止时调用"""
        pass
    
    # ==================== 数据回调 ====================
    
    def on_tick(self, price: float, timestamp: int) -> Optional[Signal]:
        """
        逐笔价格更新回调
        
        Args:
            price: 最新价格
            timestamp: 时间戳 (毫秒)
            
        Returns:
            Signal 或 None
        """
        return None
    
    @abstractmethod
    def on_candle(self, candle: Candlestick) -> Optional[Signal]:
        """
        K 线收盘回调
        
        必须实现此方法。
        
        Args:
            candle: K 线数据
            
        Returns:
            Signal 或 None
        """
        ...
    
    def on_orderbook(self, orderbook: OrderBook) -> Optional[Signal]:
        """
        订单簿更新回调
        
        Args:
            orderbook: 订单簿数据
            
        Returns:
            Signal 或 None
        """
        return None
    
    def on_trade(self, trade: Trade) -> Optional[Signal]:
        """
        成交更新回调
        
        Args:
            trade: 成交数据
            
        Returns:
            Signal 或 None
        """
        return None
    
    # ==================== 辅助方法 ====================
    
    def _emit_signal(self, signal: Signal) -> Signal:
        """记录并返回信号"""
        signal.source = self.name
        self._last_signal = signal
        return signal
