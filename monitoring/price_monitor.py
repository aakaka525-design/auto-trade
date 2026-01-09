"""
价格异常监控

检测价格快速拉升或暴跌，发送警报。
"""
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable, List
from dataclasses import dataclass, field
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class PriceAlert:
    """价格警报"""
    alert_type: str  # "pump" 拉升 / "dump" 暴跌
    symbol: str
    price_from: float
    price_to: float
    change_pct: float
    time_window_sec: float
    timestamp: datetime = field(default_factory=datetime.now)
    
    def __str__(self):
        emoji = "🚀" if self.alert_type == "pump" else "💥"
        direction = "拉升" if self.alert_type == "pump" else "暴跌"
        return f"{emoji} {direction} {self.change_pct:+.2f}% | ${self.price_from:,.2f} → ${self.price_to:,.2f}"


class PriceMonitor:
    """
    价格异常监控器
    
    检测短时间内的价格快速变化（拉升/暴跌）。
    
    使用示例:
    ```python
    monitor = PriceMonitor(
        pump_threshold_pct=1.0,   # 1% 拉升警报
        dump_threshold_pct=-1.0,  # -1% 暴跌警报
        time_window_sec=60,       # 60秒窗口
    )
    
    # 在价格更新时调用
    monitor.update(current_price)
    ```
    """
    
    def __init__(
        self,
        pump_threshold_pct: float = 1.0,
        dump_threshold_pct: float = -1.0,
        time_window_sec: float = 60.0,
        cooldown_sec: float = 300.0,
        on_alert: Optional[Callable[[PriceAlert], None]] = None,
        symbol: str = "ETH-USDC",
    ):
        self.pump_threshold_pct = pump_threshold_pct
        self.dump_threshold_pct = dump_threshold_pct
        self.time_window_sec = time_window_sec
        self.cooldown_sec = cooldown_sec
        self._on_alert = on_alert
        self.symbol = symbol
        
        # 价格历史 [(timestamp, price), ...]
        self._price_history: deque = deque(maxlen=1000)
        
        # 冷却
        self._last_pump_alert: Optional[datetime] = None
        self._last_dump_alert: Optional[datetime] = None
        
        # 统计
        self._total_alerts = 0
    
    def reset(self):
        """重置价格历史（重连时调用）"""
        self._price_history.clear()
        self._last_pump_alert = None
        self._last_dump_alert = None
    
    def update(self, price: float) -> Optional[PriceAlert]:
        """
        更新价格，检测异常
        
        Returns:
            如果检测到异常，返回 PriceAlert
        """
        now = datetime.now()
        self._price_history.append((now, price))
        
        # 清理过期数据
        cutoff = now - timedelta(seconds=self.time_window_sec * 2)
        while self._price_history and self._price_history[0][0] < cutoff:
            self._price_history.popleft()
        
        # 获取窗口内最低/最高价
        window_start = now - timedelta(seconds=self.time_window_sec)
        window_prices = [p for t, p in self._price_history if t >= window_start]
        
        if len(window_prices) < 2:
            return None
        
        min_price = min(window_prices)
        max_price = max(window_prices)
        
        # 检测拉升 (从最低到当前)
        if min_price > 0:
            pump_pct = (price - min_price) / min_price * 100
            if pump_pct >= self.pump_threshold_pct:
                if self._can_alert("pump", now):
                    alert = PriceAlert(
                        alert_type="pump",
                        symbol=self.symbol,
                        price_from=min_price,
                        price_to=price,
                        change_pct=pump_pct,
                        time_window_sec=self.time_window_sec,
                        timestamp=now,
                    )
                    self._trigger_alert(alert, now)
                    return alert
        
        # 检测暴跌 (从最高到当前)
        if max_price > 0:
            dump_pct = (price - max_price) / max_price * 100
            if dump_pct <= self.dump_threshold_pct:
                if self._can_alert("dump", now):
                    alert = PriceAlert(
                        alert_type="dump",
                        symbol=self.symbol,
                        price_from=max_price,
                        price_to=price,
                        change_pct=dump_pct,
                        time_window_sec=self.time_window_sec,
                        timestamp=now,
                    )
                    self._trigger_alert(alert, now)
                    return alert
        
        return None
    
    def _can_alert(self, alert_type: str, now: datetime) -> bool:
        """检查是否可以发送警报（冷却检查）"""
        if alert_type == "pump":
            if self._last_pump_alert:
                elapsed = (now - self._last_pump_alert).total_seconds()
                if elapsed < self.cooldown_sec:
                    return False
        else:
            if self._last_dump_alert:
                elapsed = (now - self._last_dump_alert).total_seconds()
                if elapsed < self.cooldown_sec:
                    return False
        return True
    
    def _trigger_alert(self, alert: PriceAlert, now: datetime):
        """触发警报"""
        if alert.alert_type == "pump":
            self._last_pump_alert = now
        else:
            self._last_dump_alert = now
        
        self._total_alerts += 1
        logger.warning(f"价格警报: {alert}")
        
        if self._on_alert:
            try:
                self._on_alert(alert)
            except Exception as e:
                logger.error(f"警报回调错误: {e}")
    
    def get_stats(self) -> dict:
        """获取统计"""
        return {
            "total_alerts": self._total_alerts,
            "history_size": len(self._price_history),
            "current_price": self._price_history[-1][1] if self._price_history else None,
        }
