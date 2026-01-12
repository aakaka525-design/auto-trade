"""
基差监控模块 (Basis Tracker v1.0)

监控同一币种的现货和合约价格差异，当基差超过阈值时发出警报。

基差计算:
    Basis = (合约价格 - 现货价格) / 现货价格 × 100%

告警场景:
    - 正溢价高 (> +1%): 合约价格高于现货，可能有做空套利机会
    - 负溢价高 (< -1%): 合约价格低于现货，可能有做多套利机会

使用方法:
```python
from monitoring.basis_tracker import get_basis_tracker

tracker = get_basis_tracker()

# 更新价格 (从 WebSocket 接收数据时调用)
tracker.update_price("BTCUSDT", 95000.0, is_futures=False)
tracker.update_price("BTCUSDT", 95500.0, is_futures=True)

# 获取待处理的警报
alerts = tracker.get_pending_alerts()
for alert in alerts:
    print(f"{alert.symbol}: 基差 {alert.basis_pct:+.2f}%")
```
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime, timezone
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class BasisAlert:
    """基差警报"""
    symbol: str
    basis_pct: float           # 基差百分比
    spot_price: float          # 现货价格
    futures_price: float       # 合约价格
    direction: str             # "premium" (正溢价) 或 "discount" (负溢价)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    @property
    def trigger_reason(self) -> str:
        """获取通俗化的触发原因"""
        if self.basis_pct >= 2.0:
            return "合约高溢价: 极强"
        elif self.basis_pct >= 1.0:
            return "合约高溢价: 强"
        elif self.basis_pct <= -2.0:
            return "合约折价: 极强"
        elif self.basis_pct <= -1.0:
            return "合约折价: 强"
        else:
            return f"基差: {self.basis_pct:+.2f}%"


@dataclass
class SymbolBasisState:
    """单个币种的基差状态"""
    symbol: str
    spot_price: Optional[float] = None
    futures_price: Optional[float] = None
    spot_update_time: Optional[datetime] = None
    futures_update_time: Optional[datetime] = None
    last_alert_time: Optional[datetime] = None
    last_basis_pct: float = 0.0


class BasisTracker:
    """
    基差追踪器
    
    监控现货和合约价格差异，当基差超过阈值时生成警报。
    """
    
    # 默认配置
    DEFAULT_ALERT_THRESHOLD = 1.0   # 警报阈值 (%)
    DEFAULT_COOLDOWN_SECONDS = 300  # 冷却时间 (秒)
    DEFAULT_STALE_SECONDS = 60      # 价格过期时间 (秒)
    MAX_PENDING_ALERTS = 50
    
    def __init__(
        self,
        alert_threshold: float = DEFAULT_ALERT_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
    ):
        """
        初始化基差追踪器
        
        Args:
            alert_threshold: 基差百分比触发阈值 (默认 1.0%)
            cooldown_seconds: 同一币种警报冷却时间 (默认 300 秒)
            stale_seconds: 价格过期时间，超过此时间的价格不参与计算 (默认 60 秒)
        """
        self.alert_threshold = alert_threshold
        self.cooldown_seconds = cooldown_seconds
        self.stale_seconds = stale_seconds
        
        self._states: Dict[str, SymbolBasisState] = {}
        self._pending_alerts: deque = deque(maxlen=self.MAX_PENDING_ALERTS)
    
    def _get_state(self, symbol: str) -> SymbolBasisState:
        """获取或创建币种状态"""
        if symbol not in self._states:
            self._states[symbol] = SymbolBasisState(symbol=symbol)
        return self._states[symbol]
    
    def _normalize_symbol(self, symbol: str) -> str:
        """
        标准化币种名称
        
        将 "spot:BTCUSDT" 和 "futures:BTCUSDT" 统一为 "BTCUSDT"
        """
        if ":" in symbol:
            return symbol.split(":", 1)[1]
        return symbol
    
    def update_price(
        self, 
        symbol: str, 
        price: float, 
        is_futures: bool
    ) -> Optional[BasisAlert]:
        """
        更新价格并检查是否触发警报
        
        Args:
            symbol: 币种名称 (如 "BTCUSDT" 或 "spot:BTCUSDT")
            price: 价格
            is_futures: 是否为合约价格
        
        Returns:
            如果触发警报，返回 BasisAlert；否则返回 None
        """
        if price <= 0:
            return None
        
        symbol = self._normalize_symbol(symbol)
        state = self._get_state(symbol)
        now = datetime.now(timezone.utc)
        
        # 更新价格
        if is_futures:
            state.futures_price = price
            state.futures_update_time = now
        else:
            state.spot_price = price
            state.spot_update_time = now
        
        # 检查是否可以计算基差
        return self._check_alert(symbol)
    
    def _check_alert(self, symbol: str) -> Optional[BasisAlert]:
        """检查是否触发基差警报"""
        state = self._states.get(symbol)
        if not state:
            return None
        
        # 检查价格是否都存在
        if state.spot_price is None or state.futures_price is None:
            return None
        
        now = datetime.now(timezone.utc)
        
        # 检查价格是否过期
        if state.spot_update_time:
            spot_age = (now - state.spot_update_time).total_seconds()
            if spot_age > self.stale_seconds:
                return None
        
        if state.futures_update_time:
            futures_age = (now - state.futures_update_time).total_seconds()
            if futures_age > self.stale_seconds:
                return None
        
        # 计算基差
        basis_pct = (state.futures_price - state.spot_price) / state.spot_price * 100
        state.last_basis_pct = basis_pct
        
        # 检查是否超过阈值
        if abs(basis_pct) < self.alert_threshold:
            return None
        
        # 检查冷却时间
        if state.last_alert_time:
            cooldown_elapsed = (now - state.last_alert_time).total_seconds()
            if cooldown_elapsed < self.cooldown_seconds:
                return None
        
        # 生成警报
        direction = "premium" if basis_pct > 0 else "discount"
        alert = BasisAlert(
            symbol=symbol,
            basis_pct=basis_pct,
            spot_price=state.spot_price,
            futures_price=state.futures_price,
            direction=direction,
        )
        
        state.last_alert_time = now
        self._pending_alerts.append(alert)
        
        logger.info(
            f"📊 基差警报 | {symbol} | {direction} | "
            f"基差 {basis_pct:+.2f}% | "
            f"现货 ${state.spot_price:,.0f} | 合约 ${state.futures_price:,.0f}"
        )
        
        return alert
    
    def get_pending_alerts(self) -> List[BasisAlert]:
        """获取并清空待处理的警报"""
        alerts = list(self._pending_alerts)
        self._pending_alerts.clear()
        return alerts
    
    def get_basis(self, symbol: str) -> Optional[float]:
        """获取指定币种的当前基差百分比"""
        symbol = self._normalize_symbol(symbol)
        state = self._states.get(symbol)
        if state and state.spot_price and state.futures_price:
            return (state.futures_price - state.spot_price) / state.spot_price * 100
        return None
    
    def get_all_basis(self) -> Dict[str, float]:
        """获取所有币种的基差"""
        result = {}
        for symbol, state in self._states.items():
            if state.spot_price and state.futures_price:
                basis = (state.futures_price - state.spot_price) / state.spot_price * 100
                result[symbol] = basis
        return result
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        all_basis = self.get_all_basis()
        return {
            "tracked_symbols": len(self._states),
            "active_pairs": len(all_basis),  # 同时有现货和合约价格的
            "pending_alerts": len(self._pending_alerts),
            "max_basis": max(all_basis.values()) if all_basis else 0,
            "min_basis": min(all_basis.values()) if all_basis else 0,
        }
    
    def reset(self):
        """重置追踪器"""
        self._states.clear()
        self._pending_alerts.clear()


# 单例
_basis_tracker: Optional[BasisTracker] = None


def get_basis_tracker() -> BasisTracker:
    """获取全局基差追踪器单例"""
    global _basis_tracker
    if _basis_tracker is None:
        _basis_tracker = BasisTracker()
    return _basis_tracker


def reset_basis_tracker():
    """重置全局基差追踪器"""
    global _basis_tracker
    _basis_tracker = None
