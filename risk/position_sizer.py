"""
风险管理模块 - 凯利公式仓位计算 + 硬止损
"""
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, date

from config import settings
from core.signal_parser import TradingSignal


@dataclass
class PositionSizing:
    """仓位计算结果"""
    should_trade: bool
    position_size_usdc: float
    stop_loss_price: float
    take_profit_price: float
    rejection_reason: Optional[str] = None


class PositionSizer:
    """风险管理器"""
    
    def __init__(
        self,
        max_position_usdc: float = settings.MAX_POSITION_SIZE_USDC,
        max_loss_per_trade_pct: float = settings.MAX_LOSS_PER_TRADE_PCT,
        max_daily_loss_pct: float = settings.MAX_DAILY_LOSS_PCT,
        min_confidence: float = settings.MIN_CONFIDENCE_THRESHOLD
    ):
        self.max_position_usdc = max_position_usdc
        self.max_loss_per_trade_pct = max_loss_per_trade_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.min_confidence = min_confidence
        
        # 追踪当日亏损
        self.daily_loss_usdc = 0.0
        self.daily_pnl_pct = 0.0
        self._last_reset_date: Optional[date] = None
    
    def calculate_position(
        self,
        signal: TradingSignal,
        current_price: float,
        available_balance: float,
        win_rate: float = 0.55  # 历史胜率
    ) -> PositionSizing:
        """
        使用凯利公式计算最优仓位
        
        凯利公式: f* = (bp - q) / b
        - f*: 最优下注比例
        - b: 赔率 (take_profit / stop_loss)
        - p: 胜率
        - q: 败率 (1-p)
        
        Args:
            signal: AI 交易信号
            current_price: 当前价格
            available_balance: 可用余额
            win_rate: 历史胜率
            
        Returns:
            PositionSizing 对象
        """
        # 自动重置每日统计
        self._auto_reset_daily_stats()
        
        # 1. 基础检查
        if signal.action == "hold":
            return PositionSizing(
                should_trade=False,
                position_size_usdc=0,
                stop_loss_price=0,
                take_profit_price=0,
                rejection_reason="AI 建议持仓观望"
            )
        
        # 2. 置信度检查
        if signal.confidence < self.min_confidence:
            return PositionSizing(
                should_trade=False,
                position_size_usdc=0,
                stop_loss_price=0,
                take_profit_price=0,
                rejection_reason=f"置信度 {signal.confidence:.2f} < 阈值 {self.min_confidence}"
            )
        
        # 3. 当日亏损检查
        if abs(self.daily_pnl_pct) >= self.max_daily_loss_pct:
            return PositionSizing(
                should_trade=False,
                position_size_usdc=0,
                stop_loss_price=0,
                take_profit_price=0,
                rejection_reason=f"当日亏损已达 {self.daily_pnl_pct:.2f}%，暂停交易"
            )
        
        # 4. 凯利公式计算
        stop_loss_pct = signal.stop_loss_pct / 100
        take_profit_pct = signal.take_profit_pct / 100
        
        # 防止除零
        if stop_loss_pct <= 0:
            stop_loss_pct = 0.02  # 默认 2%
        
        odds = take_profit_pct / stop_loss_pct  # b = 赔率
        p = win_rate * signal.confidence  # 用 AI 置信度调整胜率
        q = 1 - p
        
        kelly_fraction = (odds * p - q) / odds
        
        # 5. 凯利比例调整（使用半凯利策略，更保守）
        kelly_fraction = max(0, kelly_fraction) * 0.5
        
        # 6. 计算实际仓位
        kelly_position = available_balance * kelly_fraction
        
        # 应用最大仓位限制
        position_size = min(
            kelly_position,
            self.max_position_usdc,
            available_balance * 0.3  # 最多用 30% 资金
        )
        
        # 确保止损不超过最大单笔亏损
        max_loss_usdc = available_balance * (self.max_loss_per_trade_pct / 100)
        if position_size * stop_loss_pct > max_loss_usdc:
            position_size = max_loss_usdc / stop_loss_pct
        
        # 7. 计算止损止盈价格
        if signal.action == "buy":
            stop_loss_price = current_price * (1 - stop_loss_pct)
            take_profit_price = current_price * (1 + take_profit_pct)
        else:  # sell
            stop_loss_price = current_price * (1 + stop_loss_pct)
            take_profit_price = current_price * (1 - take_profit_pct)
        
        # 最小订单检查
        min_order_size = 10.0  # 最小 10 USDC
        if position_size < min_order_size:
            return PositionSizing(
                should_trade=False,
                position_size_usdc=0,
                stop_loss_price=0,
                take_profit_price=0,
                rejection_reason=f"计算仓位 {position_size:.2f} USDC < 最小订单 {min_order_size}"
            )
        
        return PositionSizing(
            should_trade=True,
            position_size_usdc=round(position_size, 2),
            stop_loss_price=round(stop_loss_price, 2),
            take_profit_price=round(take_profit_price, 2)
        )
    
    def update_daily_pnl(self, pnl_usdc: float, balance: float):
        """更新当日盈亏统计"""
        self.daily_loss_usdc += pnl_usdc
        if balance > 0:
            self.daily_pnl_pct = (self.daily_loss_usdc / balance) * 100
    
    def reset_daily_stats(self):
        """手动重置当日统计"""
        self.daily_loss_usdc = 0.0
        self.daily_pnl_pct = 0.0
        self._last_reset_date = date.today()
    
    def _auto_reset_daily_stats(self):
        """每日自动重置"""
        today = date.today()
        if self._last_reset_date != today:
            self.reset_daily_stats()
