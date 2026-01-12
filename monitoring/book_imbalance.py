"""
深度不平衡检测模块 (WBI-Lite v3.1)

v3.0 新特性:
1. 确认机制: 触发后等待 3 tick，方向一致才真正告警
2. 冷却时间: 同一币种 60 秒内只告警 1 次
3. 通俗日志: delta=+0.76 → 变化强度: 强

使用方法:
```python
from monitoring.book_imbalance import get_book_imbalance_analyzer

analyzer = get_book_imbalance_analyzer()
signal = analyzer.get_signal("market:symbol", bids, asks)
if signal.is_significant:
    print(f"{signal.direction}: {signal.trigger_reason}")
```
"""
import heapq
import logging
import math
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class ImbalanceDirection(str, Enum):
    """不平衡方向"""
    BUY_PRESSURE = "buy_pressure"
    SELL_PRESSURE = "sell_pressure"
    BALANCED = "balanced"


class AlertState(str, Enum):
    """告警状态"""
    INACTIVE = "inactive"
    PENDING = "pending"      # 新增: 等待确认
    ACTIVE = "active"
    WARMUP = "warmup"
    CROSS_MARKET = "cross"


@dataclass
class ImbalanceSignal:
    """深度不平衡信号"""
    symbol: str
    direction: ImbalanceDirection
    score: float
    delta: float
    buy_power: float
    sell_power: float
    ratio: float
    state: AlertState
    is_significant: bool
    trigger_reason: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class SymbolState:
    """单个币种的状态"""
    symbol: str
    ema_score: Optional[float] = None
    alert_state: AlertState = AlertState.WARMUP
    alert_direction: Optional[ImbalanceDirection] = None
    tick_count: int = 0
    warmup_just_ended: bool = False
    last_update: Optional[datetime] = None
    
    # v3.0: 确认机制
    pending_direction: Optional[ImbalanceDirection] = None
    pending_ticks: int = 0
    pending_trigger: str = ""
    
    # v3.1: Flip 恢复原状态
    pre_flip_direction: Optional[ImbalanceDirection] = None  # Flip 前的方向
    
    # 冷却时间
    last_alert_time: Optional[datetime] = None


class BookImbalanceAnalyzer:
    """
    深度不平衡分析器 (WBI-Lite v3.0)
    
    触发条件 (OR):
    1. Edge Trigger: |Delta| >= delta_trigger
    2. Level Trigger: |Score| >= level_trigger
    
    确认机制:
    - 触发 → PENDING 状态 → 等待 N tick → 方向仍一致 → 真正告警
    - 如果中途方向变化，取消告警
    
    冷却时间:
    - 同一币种 M 秒内只告警 1 次
    """
    
    MAX_PENDING_SIGNALS = 100
    MAX_SYMBOLS = 3000
    SYMBOL_TTL_SECONDS = 3600
    
    def __init__(
        self,
        max_depth: int = 10,
        ema_alpha: float = 0.1,
        delta_trigger: float = 0.7,       # v3.0: 提高阈值
        delta_reset: float = 0.2,
        level_trigger: float = 0.85,      # v3.0: 提高阈值
        direction_threshold: float = 0.1,
        warmup_ticks: int = 10,
        confirm_ticks: int = 3,           # v3.0: 确认所需 tick 数
        cooldown_seconds: float = 60.0,   # v3.0: 冷却时间
        min_spread_bps: float = 1.0,
        max_spread_bps: float = 500.0,
        sigmoid_gain: float = 2.0,
    ):
        self.max_depth = max_depth
        self.ema_alpha = ema_alpha
        self.delta_trigger = delta_trigger
        self.delta_reset = delta_reset
        self.level_trigger = level_trigger
        self.direction_threshold = direction_threshold
        self.warmup_ticks = warmup_ticks
        self.confirm_ticks = confirm_ticks
        self.cooldown_seconds = cooldown_seconds
        self.min_spread_bps = min_spread_bps
        self.max_spread_bps = max_spread_bps
        self.sigmoid_gain = sigmoid_gain
        
        self._states: Dict[str, SymbolState] = {}
        self._pending_signals: deque = deque(maxlen=self.MAX_PENDING_SIGNALS)
        self._last_cleanup: datetime = datetime.now(timezone.utc)
    
    CLEANUP_COOLDOWN = 60
    
    def _get_state(self, symbol: str) -> SymbolState:
        now = datetime.now(timezone.utc)
        if len(self._states) > self.MAX_SYMBOLS:
            if (now - self._last_cleanup).total_seconds() > self.CLEANUP_COOLDOWN:
                self._cleanup_zombie_states(exclude_symbol=symbol)
                self._last_cleanup = now
        
        if symbol not in self._states:
            self._states[symbol] = SymbolState(symbol=symbol)
        return self._states[symbol]
    
    def _cleanup_zombie_states(self, exclude_symbol: str = None):
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - self.SYMBOL_TTL_SECONDS
        
        expired = set(
            symbol for symbol, state in self._states.items()
            if symbol != exclude_symbol
            and state.last_update 
            and state.last_update.timestamp() < cutoff
        )
        
        if len(self._states) - len(expired) > self.MAX_SYMBOLS * 0.9:
            candidates = [s for s in self._states.keys() if s != exclude_symbol and s not in expired]
            delete_count = len(self._states) // 10
            if candidates and delete_count > 0:
                expired.update(random.sample(candidates, min(delete_count, len(candidates))))
        
        for symbol in expired:
            del self._states[symbol]
        
        if expired:
            logger.debug(f"WBI 清理 {len(expired)} 个 Symbol")
    
    def _get_top_levels(
        self, 
        bids: List[Tuple[float, float]], 
        asks: List[Tuple[float, float]]
    ) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
        top_bids = heapq.nlargest(self.max_depth, bids, key=lambda x: x[0])
        top_asks = heapq.nsmallest(self.max_depth, asks, key=lambda x: x[0])
        return top_bids, top_asks
    
    def _calculate_spread_weight(self, price: float, mid_price: float, spread: float) -> float:
        relative_dist = abs(price - mid_price) / spread
        return 1.0 / (1.0 + relative_dist)
    
    def _calculate_weighted_power(self, levels: List[Tuple[float, float]], mid_price: float, spread: float) -> float:
        total_power = 0.0
        for price, qty in levels:
            if price <= 0 or qty <= 0:
                continue
            weight = self._calculate_spread_weight(price, mid_price, spread)
            total_power += price * qty * weight
        return total_power
    
    def _log_sigmoid(self, ratio: float) -> float:
        ratio = max(0.001, min(1000.0, ratio))
        log_ratio = math.log10(ratio)
        x = self.sigmoid_gain * log_ratio
        sigmoid = 1.0 / (1.0 + math.exp(-x))
        return 2.0 * (sigmoid - 0.5)
    
    def _get_direction_from_value(self, value: float) -> ImbalanceDirection:
        if value > self.direction_threshold:
            return ImbalanceDirection.BUY_PRESSURE
        elif value < -self.direction_threshold:
            return ImbalanceDirection.SELL_PRESSURE
        return ImbalanceDirection.BALANCED
    
    def _is_in_cooldown(self, state: SymbolState) -> bool:
        """检查是否在冷却期"""
        if state.last_alert_time is None:
            return False
        elapsed = (datetime.now(timezone.utc) - state.last_alert_time).total_seconds()
        return elapsed < self.cooldown_seconds
    
    def _format_trigger_reason(self, trigger_type: str, value: float) -> str:
        """格式化触发原因为通俗表达"""
        abs_val = abs(value)
        
        if abs_val >= 1.0:
            strength = "极强"
        elif abs_val >= 0.8:
            strength = "强"
        elif abs_val >= 0.6:
            strength = "中等"
        else:
            strength = "弱"
        
        if trigger_type == "delta":
            return f"变化: {strength}"
        elif trigger_type == "level":
            return f"不平衡: {strength}"
        elif trigger_type == "flip":
            return f"反转: {strength}"
        else:
            return trigger_type
    
    def get_signal(
        self, 
        symbol: str, 
        bids: List[Tuple[float, float]], 
        asks: List[Tuple[float, float]]
    ) -> ImbalanceSignal:
        """计算深度不平衡信号"""
        state = self._get_state(symbol)
        state.tick_count += 1
        state.last_update = datetime.now(timezone.utc)
        
        # 数据检查
        if not bids or not asks:
            return self._empty_signal(symbol, state, "no_data")
        
        top_bids, top_asks = self._get_top_levels(bids, asks)
        
        if not top_bids or not top_asks:
            return self._empty_signal(symbol, state, "no_data")
        
        best_bid = top_bids[0][0]
        best_ask = top_asks[0][0]
        
        # 倒挂检测
        if best_bid >= best_ask:
            state.alert_state = AlertState.CROSS_MARKET
            state.pending_direction = None
            state.pending_ticks = 0
            return self._empty_signal(symbol, state, "cross_market")
        
        mid_price = (best_bid + best_ask) / 2
        raw_spread = best_ask - best_bid
        min_spread = mid_price * self.min_spread_bps / 10000
        max_spread = mid_price * self.max_spread_bps / 10000
        spread = max(min_spread, min(raw_spread, max_spread))
        
        buy_power = self._calculate_weighted_power(top_bids, mid_price, spread)
        sell_power = self._calculate_weighted_power(top_asks, mid_price, spread)
        
        epsilon = 1e-8
        ratio = (buy_power + epsilon) / (sell_power + epsilon)
        score = self._log_sigmoid(ratio)
        
        # 热身期
        if state.tick_count <= self.warmup_ticks:
            return ImbalanceSignal(
                symbol=symbol, direction=ImbalanceDirection.BALANCED,
                score=score, delta=0.0, buy_power=buy_power, sell_power=sell_power,
                ratio=ratio, state=AlertState.WARMUP, is_significant=False, trigger_reason="warmup",
            )
        
        # 热身刚结束
        if state.alert_state == AlertState.WARMUP:
            state.alert_state = AlertState.INACTIVE
            state.ema_score = score
            state.warmup_just_ended = True
        
        # CROSS 恢复
        cross_just_recovered = False
        if state.alert_state == AlertState.CROSS_MARKET:
            state.alert_state = AlertState.INACTIVE
            state.ema_score = score
            cross_just_recovered = True
        
        # 计算 Delta
        if state.warmup_just_ended or cross_just_recovered:
            delta = 0.0
            state.warmup_just_ended = False
        else:
            delta = score - state.ema_score if state.ema_score else 0.0
        
        # 更新 EMA
        if state.ema_score is None:
            state.ema_score = score
        else:
            state.ema_score = self.ema_alpha * score + (1 - self.ema_alpha) * state.ema_score
        
        # === v3.0 状态机 (带确认机制) ===
        is_significant = False
        trigger_reason = ""
        new_state = state.alert_state
        direction = ImbalanceDirection.BALANCED
        
        abs_delta = abs(delta)
        abs_score = abs(score)
        
        # 当前方向判断
        current_direction = self._get_direction_from_value(delta if abs_delta > 0.1 else score)
        
        if state.alert_state == AlertState.INACTIVE:
            # 检测触发条件
            triggered = False
            trigger_type = ""
            trigger_value = 0.0
            
            if abs_delta >= self.delta_trigger:
                triggered = True
                trigger_type = "delta"
                trigger_value = delta
                direction = ImbalanceDirection.BUY_PRESSURE if delta > 0 else ImbalanceDirection.SELL_PRESSURE
            elif abs_score >= self.level_trigger:
                triggered = True
                trigger_type = "level"
                trigger_value = score
                direction = ImbalanceDirection.BUY_PRESSURE if score > 0 else ImbalanceDirection.SELL_PRESSURE
            
            if triggered:
                # 进入 PENDING 状态，等待确认
                new_state = AlertState.PENDING
                state.pending_direction = direction
                state.pending_ticks = 1
                state.pending_trigger = self._format_trigger_reason(trigger_type, trigger_value)
        
        elif state.alert_state == AlertState.PENDING:
            # 确认中：检查方向是否一致
            pending_dir = state.pending_direction
            
            # 计算当前方向
            if abs_delta >= self.delta_trigger:
                current_dir = ImbalanceDirection.BUY_PRESSURE if delta > 0 else ImbalanceDirection.SELL_PRESSURE
            elif abs_score >= self.level_trigger:
                current_dir = ImbalanceDirection.BUY_PRESSURE if score > 0 else ImbalanceDirection.SELL_PRESSURE
            else:
                current_dir = self._get_direction_from_value(score)
            
            # 方向一致？
            if current_dir == pending_dir:
                state.pending_ticks += 1
                
                if state.pending_ticks >= self.confirm_ticks:
                    # 确认成功！检查冷却
                    if not self._is_in_cooldown(state):
                        is_significant = True
                        direction = pending_dir
                        trigger_reason = state.pending_trigger
                        new_state = AlertState.ACTIVE
                        state.alert_direction = direction
                        state.last_alert_time = datetime.now(timezone.utc)
                    else:
                        # v3.1: 冷却中，记录原因
                        new_state = AlertState.ACTIVE
                        state.alert_direction = pending_dir
                        direction = pending_dir
                        trigger_reason = f"{state.pending_trigger} (冷却中)"
                    # 清理 pre_flip_direction
                    state.pre_flip_direction = None
            else:
                # v3.1: 方向变了，检查是否是强反向信号
                if abs_delta >= self.delta_trigger or abs_score >= self.level_trigger:
                    # 强反向信号，直接切换到反向 PENDING
                    new_state = AlertState.PENDING
                    state.pending_direction = current_dir
                    state.pending_ticks = 1
                    state.pending_trigger = self._format_trigger_reason("delta" if abs_delta >= self.delta_trigger else "level", delta if abs_delta >= self.delta_trigger else score)
                else:
                    # 弱信号，检查是否有 pre_flip_direction 可恢复
                    if state.pre_flip_direction:
                        # v3.1: 恢复到 Flip 前的 ACTIVE 状态
                        new_state = AlertState.ACTIVE
                        state.alert_direction = state.pre_flip_direction
                        direction = state.pre_flip_direction
                        state.pre_flip_direction = None
                    else:
                        # 无法恢复，重置为 INACTIVE
                        new_state = AlertState.INACTIVE
                        state.pending_direction = None
                        state.pending_ticks = 0
                        state.pending_trigger = ""
        
        elif state.alert_state == AlertState.ACTIVE:
            old_direction = state.alert_direction
            
            # Edge Flip
            edge_flip = (
                (old_direction == ImbalanceDirection.BUY_PRESSURE and delta < -self.delta_trigger) or
                (old_direction == ImbalanceDirection.SELL_PRESSURE and delta > self.delta_trigger)
            )
            
            # Level Flip
            level_flip = (
                (old_direction == ImbalanceDirection.BUY_PRESSURE and score < -self.level_trigger) or
                (old_direction == ImbalanceDirection.SELL_PRESSURE and score > self.level_trigger)
            )
            
            if edge_flip or level_flip:
                # v3.1: 进入反转确认，保存原状态以便恢复
                new_direction = ImbalanceDirection.BUY_PRESSURE if (delta > 0 or score > 0) else ImbalanceDirection.SELL_PRESSURE
                new_state = AlertState.PENDING
                state.pre_flip_direction = old_direction  # 保存原方向
                state.pending_direction = new_direction
                state.pending_ticks = 1
                state.pending_trigger = self._format_trigger_reason("flip", delta if edge_flip else score)
                direction = old_direction  # 暂时保持旧方向
            
            elif abs_delta < self.delta_reset and abs_score < self.level_trigger * 0.7:
                # 复位
                new_state = AlertState.INACTIVE
                state.alert_direction = None
                state.pending_direction = None
                state.pending_ticks = 0
                state.pre_flip_direction = None
            else:
                direction = state.alert_direction or ImbalanceDirection.BALANCED
        
        state.alert_state = new_state
        
        signal = ImbalanceSignal(
            symbol=symbol, direction=direction, score=score, delta=delta,
            buy_power=buy_power, sell_power=sell_power, ratio=ratio,
            state=new_state, is_significant=is_significant, trigger_reason=trigger_reason,
        )
        
        if is_significant:
            self._pending_signals.append(signal)
        
        return signal
    
    def _empty_signal(self, symbol: str, state: SymbolState, reason: str) -> ImbalanceSignal:
        return ImbalanceSignal(
            symbol=symbol, direction=ImbalanceDirection.BALANCED, score=0.0, delta=0.0,
            buy_power=0.0, sell_power=0.0, ratio=1.0, state=state.alert_state,
            is_significant=False, trigger_reason=reason,
        )
    
    def get_pending_signals(self) -> List[ImbalanceSignal]:
        signals = list(self._pending_signals)
        self._pending_signals.clear()
        return signals
    
    def get_stats(self) -> Dict:
        return {
            "total_symbols": len(self._states),
            "active_alerts": sum(1 for s in self._states.values() if s.alert_state == AlertState.ACTIVE),
            "pending_alerts": sum(1 for s in self._states.values() if s.alert_state == AlertState.PENDING),
            "warmup_symbols": sum(1 for s in self._states.values() if s.alert_state == AlertState.WARMUP),
            "cross_market": sum(1 for s in self._states.values() if s.alert_state == AlertState.CROSS_MARKET),
            "pending_signals": len(self._pending_signals),
        }
    
    def reset(self):
        self._states.clear()
        self._pending_signals.clear()


_book_imbalance_analyzer: Optional[BookImbalanceAnalyzer] = None


def get_book_imbalance_analyzer() -> BookImbalanceAnalyzer:
    global _book_imbalance_analyzer
    if _book_imbalance_analyzer is None:
        _book_imbalance_analyzer = BookImbalanceAnalyzer()
    return _book_imbalance_analyzer


def reset_book_imbalance_analyzer():
    global _book_imbalance_analyzer
    _book_imbalance_analyzer = None
