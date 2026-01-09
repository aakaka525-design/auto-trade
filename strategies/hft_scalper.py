"""
高频 Scalping 策略模板

基于订单簿微观结构的快速交易策略。
适用于做市、价差套利等场景。
"""
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Deque

from connectors.base import OrderBook, Candlestick
from strategies.base import BaseStrategy, Signal, SignalAction

logger = logging.getLogger(__name__)


# ==================== 策略配置 ====================

@dataclass
class ScalperConfig:
    """Scalper 策略配置"""
    
    # 触发阈值
    spread_threshold_pct: float = 0.001  # 0.1% 价差触发
    imbalance_threshold: float = 0.3  # 30% 买卖不平衡
    
    # 风险控制
    max_position_size: float = 1.0  # 最大持仓
    min_edge_pct: float = 0.0002  # 最小边缘 (扣除手续费后)
    
    # 做市参数
    quote_depth: int = 3  # 报价参考深度
    order_size_pct: float = 0.1  # 单次下单占最大仓位比例
    
    # 时间控制
    min_signal_interval_ms: int = 100  # 最小信号间隔
    
    # 止盈止损
    take_profit_pct: float = 0.003  # 0.3% 止盈
    stop_loss_pct: float = 0.002  # 0.2% 止损


# ==================== 策略实现 ====================

class HFTScalperStrategy(BaseStrategy):
    """
    高频订单簿 Scalping 策略
    
    信号触发条件:
    1. 买卖价差大于阈值 (存在套利空间)
    2. 订单簿不平衡度达标 (预测短期方向)
    
    使用示例:
    ```python
    strategy = HFTScalperStrategy(
        spread_threshold_pct=0.001,
        imbalance_threshold=0.3
    )
    
    # 接收订单簿更新
    signal = strategy.on_orderbook(orderbook)
    if signal and signal.is_entry:
        await engine.submit(signal, symbol="ETH-USDC", size=0.1)
    ```
    
    注意:
    - 本策略需要高频订单簿推送 (WebSocket)
    - 交易费率需低于 spread_threshold 才能盈利
    - 建议在高流动性市场使用
    """
    
    def __init__(
        self,
        spread_threshold_pct: float = 0.001,
        imbalance_threshold: float = 0.3,
        max_position_size: float = 1.0,
        **kwargs,
    ):
        super().__init__("hft_scalper")
        
        self.config = ScalperConfig(
            spread_threshold_pct=spread_threshold_pct,
            imbalance_threshold=imbalance_threshold,
            max_position_size=max_position_size,
            **kwargs,
        )
        
        # 状态
        self._current_position: float = 0.0
        self._last_signal_time: Optional[datetime] = None
        self._price_history: Deque[float] = deque(maxlen=100)
        
        # 统计
        self._signal_count = 0
        self._trade_count = 0
    
    # ==================== 核心信号逻辑 ====================
    
    def on_orderbook(self, orderbook: OrderBook) -> Optional[Signal]:
        """
        订单簿更新回调 - 主入口
        
        分析订单簿结构，判断是否存在短期交易机会。
        """
        if not self._enabled:
            return None
        
        # 检查信号频率
        if not self._check_signal_interval():
            return None
        
        # 计算指标
        spread_pct = self._calculate_spread_pct(orderbook)
        imbalance = self._calculate_imbalance(orderbook, self.config.quote_depth)
        mid_price = orderbook.mid_price
        
        if mid_price is None:
            return None
        
        # 记录价格
        self._price_history.append(mid_price)
        
        # 判断是否触发
        signal = self._evaluate_signal(
            orderbook=orderbook,
            spread_pct=spread_pct,
            imbalance=imbalance,
            mid_price=mid_price,
        )
        
        if signal:
            self._signal_count += 1
            self._last_signal_time = datetime.now()
            logger.debug(
                f"[HFT] 信号 #{self._signal_count}: "
                f"{signal.action.value} @ {mid_price:.2f} "
                f"(spread={spread_pct:.4%}, imbalance={imbalance:.2f})"
            )
        
        return signal
    
    def on_candle(self, candle: Candlestick) -> Optional[Signal]:
        """K 线回调 - Scalping 通常不使用"""
        return None
    
    # ==================== 指标计算 ====================
    
    def _calculate_spread_pct(self, ob: OrderBook) -> float:
        """
        计算买卖价差百分比
        
        spread_pct = (best_ask - best_bid) / mid_price
        """
        if not ob.best_ask or not ob.best_bid or not ob.mid_price:
            return 0.0
        
        return (ob.best_ask - ob.best_bid) / ob.mid_price
    
    def _calculate_imbalance(self, ob: OrderBook, depth: int = 3) -> float:
        """
        计算订单簿不平衡度
        
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        
        返回值范围: [-1, 1]
        - 正值: 买盘强于卖盘 (看涨)
        - 负值: 卖盘强于买盘 (看跌)
        """
        bid_volume = sum(
            level.size for level in ob.bids[:depth]
        ) if ob.bids else 0
        
        ask_volume = sum(
            level.size for level in ob.asks[:depth]
        ) if ob.asks else 0
        
        total = bid_volume + ask_volume
        if total == 0:
            return 0.0
        
        return (bid_volume - ask_volume) / total
    
    def _calculate_vwap(self, ob: OrderBook, side: str, size: float) -> float:
        """
        计算加权平均成交价 (VWAP)
        
        模拟按市价吃掉 size 数量时的平均价格。
        """
        levels = ob.asks if side == "buy" else ob.bids
        remaining = size
        total_cost = 0.0
        
        for level in levels:
            fill_size = min(remaining, level.size)
            total_cost += fill_size * level.price
            remaining -= fill_size
            
            if remaining <= 0:
                break
        
        filled = size - remaining
        return total_cost / filled if filled > 0 else 0.0
    
    # ==================== 信号评估 ====================
    
    def _evaluate_signal(
        self,
        orderbook: OrderBook,
        spread_pct: float,
        imbalance: float,
        mid_price: float,
    ) -> Optional[Signal]:
        """
        评估是否产生交易信号
        
        策略逻辑:
        1. 价差足够大 (有盈利空间)
        2. 不平衡度足够强 (有方向判断)
        3. 当前仓位允许
        """
        # 检查价差
        if spread_pct < self.config.spread_threshold_pct:
            return None
        
        # 检查不平衡度
        if abs(imbalance) < self.config.imbalance_threshold:
            return None
        
        # 确定方向
        if imbalance > 0:
            # 买盘强 -> 做多
            action = SignalAction.BUY
            target_position = self.config.max_position_size * self.config.order_size_pct
        else:
            # 卖盘强 -> 做空
            action = SignalAction.SELL
            target_position = -self.config.max_position_size * self.config.order_size_pct
        
        # 检查仓位限制
        if not self._check_position_limit(action, target_position):
            return None
        
        # 计算止盈止损
        take_profit, stop_loss = self._calculate_exits(action, mid_price)
        
        # 计算置信度 (基于不平衡度强度)
        confidence = min(abs(imbalance) / 0.5, 1.0)  # 50% 不平衡 = 100% 置信
        
        return self._emit_signal(Signal(
            action=action,
            confidence=confidence,
            price=mid_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            reason=f"Spread: {spread_pct:.4%}, Imbalance: {imbalance:.2f}",
            metadata={
                "spread_pct": spread_pct,
                "imbalance": imbalance,
                "best_bid": orderbook.best_bid,
                "best_ask": orderbook.best_ask,
            }
        ))
    
    def _check_position_limit(self, action: SignalAction, delta: float) -> bool:
        """检查仓位限制"""
        new_position = self._current_position + delta
        
        if action == SignalAction.BUY:
            return new_position <= self.config.max_position_size
        else:
            return new_position >= -self.config.max_position_size
    
    def _calculate_exits(
        self, 
        action: SignalAction, 
        entry_price: float
    ) -> tuple[float, float]:
        """计算止盈止损价格"""
        if action == SignalAction.BUY:
            take_profit = entry_price * (1 + self.config.take_profit_pct)
            stop_loss = entry_price * (1 - self.config.stop_loss_pct)
        else:
            take_profit = entry_price * (1 - self.config.take_profit_pct)
            stop_loss = entry_price * (1 + self.config.stop_loss_pct)
        
        return take_profit, stop_loss
    
    def _check_signal_interval(self) -> bool:
        """检查信号间隔是否满足"""
        if self._last_signal_time is None:
            return True
        
        elapsed_ms = (datetime.now() - self._last_signal_time).total_seconds() * 1000
        return elapsed_ms >= self.config.min_signal_interval_ms
    
    # ==================== 仓位管理 ====================
    
    def update_position(self, filled_size: float, side: SignalAction) -> None:
        """更新当前持仓 (外部调用)"""
        if side == SignalAction.BUY:
            self._current_position += filled_size
        else:
            self._current_position -= filled_size
        
        logger.info(f"[HFT] 仓位更新: {self._current_position:.4f}")
    
    def reset_position(self) -> None:
        """重置仓位"""
        self._current_position = 0.0
    
    # ==================== 状态查询 ====================
    
    def get_stats(self) -> dict:
        """获取策略统计"""
        return {
            "name": self.name,
            "enabled": self._enabled,
            "current_position": self._current_position,
            "signal_count": self._signal_count,
            "trade_count": self._trade_count,
            "config": {
                "spread_threshold_pct": self.config.spread_threshold_pct,
                "imbalance_threshold": self.config.imbalance_threshold,
                "max_position_size": self.config.max_position_size,
            }
        }
