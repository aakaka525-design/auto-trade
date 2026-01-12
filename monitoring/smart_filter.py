"""
智能告警过滤模块 v2.0

基于 SortedList 的高性能动态阈值 + TTL 时间过期机制。

特性:
1. O(log N) 增量更新 (SortedList)
2. 数量 + 时间双重窗口限制 (TTL)
3. 冷启动安全阈值
4. P95/P05 双向阈值

使用方法:
```python
from monitoring.smart_filter import get_smart_filter

filter = get_smart_filter()
filter.record_slippage("ETH-USDT", 1.5)
should_alert, reason = filter.should_alert("ETH-USDT", 5.0)
```
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, Tuple, Optional, List
from datetime import datetime, timedelta
from sortedcontainers import SortedList

logger = logging.getLogger(__name__)


@dataclass
class TimestampedValue:
    """带时间戳的值"""
    value: float
    timestamp: datetime
    
    def __lt__(self, other):
        """按 value 排序"""
        return self.value < other.value


class SymbolStats:
    """单个币种的统计数据 (使用 SortedList)"""
    
    def __init__(self, symbol: str, max_size: int = 1000, ttl_seconds: float = 3600):
        self.symbol = symbol
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        
        # SortedList 按 value 排序
        self._sorted_values: SortedList = SortedList()
        # 时间顺序队列用于 TTL 清理
        self._time_queue: List[TimestampedValue] = []
        
        # 冷却
        self.last_alert_time: Optional[datetime] = None
        self.alert_count_1h: int = 0
    
    def add(self, value: float, timestamp: datetime = None) -> None:
        """添加数据 O(log N)"""
        ts = timestamp or datetime.now()
        item = TimestampedValue(value=value, timestamp=ts)
        
        # 添加到排序列表
        self._sorted_values.add(item)
        self._time_queue.append(item)
        
        # 数量限制
        if len(self._sorted_values) > self.max_size:
            oldest = self._time_queue.pop(0)
            self._sorted_values.remove(oldest)
    
    def cleanup_expired(self) -> int:
        """清理过期数据，返回清理数量"""
        if not self._time_queue:
            return 0
        
        cutoff = datetime.now() - timedelta(seconds=self.ttl_seconds)
        removed = 0
        
        while self._time_queue and self._time_queue[0].timestamp < cutoff:
            expired = self._time_queue.pop(0)
            try:
                self._sorted_values.remove(expired)
                removed += 1
            except ValueError:
                pass  # 可能已被数量限制移除
        
        return removed
    
    def get_percentile(self, percentile: float) -> Optional[float]:
        """获取百分位数 O(1)"""
        self.cleanup_expired()
        
        if not self._sorted_values:
            return None
        
        index = int(len(self._sorted_values) * percentile / 100)
        index = min(index, len(self._sorted_values) - 1)
        return self._sorted_values[index].value
    
    @property
    def count(self) -> int:
        return len(self._sorted_values)


class SmartAlertFilter:
    """
    智能告警过滤器 v2.0
    
    特性:
    - SortedList O(log N) 增量更新
    - TTL 时间过期机制
    - 动态 P95 阈值
    - 冷启动安全阈值
    """
    
    def __init__(
        self,
        window_size: int = 1000,
        ttl_seconds: float = 3600.0,       # 1 小时过期
        percentile: float = 95.0,
        min_samples: int = 100,             # 最小样本量
        cooldown_seconds: float = 60.0,
        fallback_thresholds: Dict[str, float] = None
    ):
        self.window_size = window_size
        self.ttl_seconds = ttl_seconds
        self.percentile = percentile
        self.min_samples = min_samples
        self.cooldown_seconds = cooldown_seconds
        
        # 冷启动安全阈值
        self.fallback_thresholds = fallback_thresholds or {
            "default": 2.0,  # 一般币种
            "major": 1.5,    # 主流币
        }
        
        # 主流币列表
        self.major_symbols = {
            "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK"
        }
        
        # 各币种统计
        self._stats: Dict[str, SymbolStats] = {}
    
    def _get_stats(self, symbol: str) -> SymbolStats:
        """获取或创建币种统计"""
        if symbol not in self._stats:
            self._stats[symbol] = SymbolStats(
                symbol=symbol,
                max_size=self.window_size,
                ttl_seconds=self.ttl_seconds
            )
        return self._stats[symbol]
    
    def _get_base_symbol(self, symbol: str) -> str:
        """提取基础币种 (ETH-USDT -> ETH)"""
        return symbol.split("-")[0].split("/")[0].upper()
    
    def _is_major(self, symbol: str) -> bool:
        """判断是否为主流币"""
        base = self._get_base_symbol(symbol)
        return base in self.major_symbols
    
    def record_slippage(self, symbol: str, slippage: float) -> None:
        """记录滑点数据 O(log N)"""
        stats = self._get_stats(symbol)
        stats.add(slippage)
    
    def get_dynamic_threshold(self, symbol: str) -> Tuple[float, str]:
        """
        获取动态阈值
        
        Returns:
            (threshold, source)
        """
        stats = self._get_stats(symbol)
        
        # 样本不足，使用安全阈值
        if stats.count < self.min_samples:
            default_key = "major" if self._is_major(symbol) else "default"
            return self.fallback_thresholds[default_key], f"fallback({default_key}, n={stats.count})"
        
        # 计算 P95
        p95 = stats.get_percentile(self.percentile)
        if p95 is None:
            default_key = "major" if self._is_major(symbol) else "default"
            return self.fallback_thresholds[default_key], "fallback(no_data)"
        
        # 最小阈值保护
        min_threshold = 0.5 if self._is_major(symbol) else 1.0
        threshold = max(p95, min_threshold)
        
        return threshold, f"P{self.percentile:.0f}(n={stats.count})"
    
    def should_alert(
        self, 
        symbol: str, 
        slippage: float,
        value: float = 0
    ) -> Tuple[bool, Optional[str]]:
        """
        判断是否应该告警
        
        Returns:
            (should_alert, reason)
        """
        stats = self._get_stats(symbol)
        now = datetime.now()
        
        # 冷却期检查
        if stats.last_alert_time:
            elapsed = (now - stats.last_alert_time).total_seconds()
            if elapsed < self.cooldown_seconds:
                return False, f"cooldown({self.cooldown_seconds - elapsed:.0f}s)"
        
        # 获取动态阈值
        threshold, source = self.get_dynamic_threshold(symbol)
        
        # 比较
        if slippage >= threshold:
            stats.last_alert_time = now
            stats.alert_count_1h += 1
            return True, f"{slippage:.2f}% >= {threshold:.2f}% [{source}]"
        
        return False, f"{slippage:.2f}% < {threshold:.2f}% [{source}]"
    
    def get_stats_summary(self) -> Dict:
        """获取统计摘要"""
        summary = {
            "total_symbols": len(self._stats),
            "symbols": {}
        }
        
        for symbol, stats in self._stats.items():
            if stats.count > 0:
                threshold, source = self.get_dynamic_threshold(symbol)
                p50 = stats.get_percentile(50.0)
                p95 = stats.get_percentile(95.0)
                
                summary["symbols"][symbol] = {
                    "samples": stats.count,
                    "threshold": threshold,
                    "source": source,
                    "p50": p50,
                    "p95": p95,
                    "alert_count_1h": stats.alert_count_1h,
                }
        
        return summary
    
    def cleanup_all_expired(self) -> int:
        """清理所有币种的过期数据"""
        total_removed = 0
        for stats in self._stats.values():
            total_removed += stats.cleanup_expired()
        return total_removed


# 全局实例
_smart_filter: Optional[SmartAlertFilter] = None


def get_smart_filter() -> SmartAlertFilter:
    """获取全局智能过滤器"""
    global _smart_filter
    if _smart_filter is None:
        _smart_filter = SmartAlertFilter()
    return _smart_filter
