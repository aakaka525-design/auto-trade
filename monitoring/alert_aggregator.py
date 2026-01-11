"""
告警聚合模块

相同币种的多次告警合并为一条，减少消息数量。

使用方法:
```python
from monitoring.alert_aggregator import AlertAggregator

aggregator = AlertAggregator(window_seconds=60)

# 添加告警 (返回是否应该发送)
should_send, summary = aggregator.add_alert(
    symbol="ETH-USDT",
    level="medium",
    market="spot",
    value=50000,
    slippage=1.2
)

if should_send:
    send_telegram(summary)
```
"""
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class AlertBucket:
    """告警桶 (聚合一段时间内的同币种告警)"""
    symbol: str
    market: str
    first_time: datetime = field(default_factory=datetime.now)
    last_time: datetime = field(default_factory=datetime.now)
    count: int = 0
    total_value: float = 0.0
    max_slippage: float = 0.0
    levels: List[str] = field(default_factory=list)
    
    def add(self, value: float, slippage: float, level: str):
        """添加告警到桶"""
        self.count += 1
        self.total_value += value
        self.max_slippage = max(self.max_slippage, slippage)
        self.last_time = datetime.now()
        self.levels.append(level)
    
    def get_highest_level(self) -> str:
        """获取最高告警级别"""
        if "high" in self.levels:
            return "high"
        elif "medium" in self.levels:
            return "medium"
        return "low"
    
    def to_summary(self) -> str:
        """生成聚合摘要消息"""
        highest = self.get_highest_level()
        level_icon = {"low": "📊", "medium": "🐋", "high": "🚨"}.get(highest, "📊")
        market_tag = "📈合约" if self.market == "futures" else "💰现货"
        
        if self.count == 1:
            return f"{level_icon} {market_tag} | {self.symbol} | ${self.total_value:,.0f} | 滑点 {self.max_slippage:.2f}%"
        
        return (
            f"{level_icon} {market_tag} | {self.symbol} | "
            f"聚合 x{self.count} | 总计 ${self.total_value:,.0f} | "
            f"最大滑点 {self.max_slippage:.2f}%"
        )


class AlertAggregator:
    """
    告警聚合器
    
    特性:
    - 按币种+市场类型聚合同一时间窗口内的告警
    - 窗口结束时发送聚合消息
    - 高优先级告警立即发送
    - 线程安全
    """
    
    def __init__(
        self,
        window_seconds: float = 60.0,
        immediate_levels: List[str] = None
    ):
        """
        Args:
            window_seconds: 聚合窗口时长 (秒)
            immediate_levels: 立即发送的级别 (默认 ["high"])
        """
        self.window_seconds = window_seconds
        self.immediate_levels = immediate_levels or ["high"]
        
        self._buckets: Dict[str, AlertBucket] = {}
        self._lock = Lock()
    
    def _get_bucket_key(self, symbol: str, market: str) -> str:
        """生成桶键"""
        return f"{market}:{symbol}"
    
    def add_alert(
        self,
        symbol: str,
        level: str,
        market: str,
        value: float,
        slippage: float
    ) -> Tuple[bool, Optional[str]]:
        """
        添加告警
        
        Args:
            symbol: 币种
            level: 告警级别
            market: 市场类型 (spot/futures)
            value: 金额
            slippage: 滑点
            
        Returns:
            (should_send, summary_message)
            - should_send: 是否应该发送消息
            - summary_message: 聚合摘要消息 (如果 should_send 为 True)
        """
        # 高优先级立即发送
        if level in self.immediate_levels:
            bucket = AlertBucket(symbol=symbol, market=market)
            bucket.add(value, slippage, level)
            return True, bucket.to_summary()
        
        key = self._get_bucket_key(symbol, market)
        now = datetime.now()
        
        with self._lock:
            # 检查现有桶
            if key in self._buckets:
                bucket = self._buckets[key]
                age = (now - bucket.first_time).total_seconds()
                
                if age >= self.window_seconds:
                    # 窗口结束，发送聚合消息并创建新桶
                    summary = bucket.to_summary()
                    
                    # 创建新桶
                    new_bucket = AlertBucket(symbol=symbol, market=market)
                    new_bucket.add(value, slippage, level)
                    self._buckets[key] = new_bucket
                    
                    return True, summary
                else:
                    # 添加到现有桶
                    bucket.add(value, slippage, level)
                    return False, None
            else:
                # 创建新桶
                bucket = AlertBucket(symbol=symbol, market=market)
                bucket.add(value, slippage, level)
                self._buckets[key] = bucket
                return False, None
    
    def flush_all(self) -> List[str]:
        """
        强制刷新所有桶 (关闭时调用)
        
        Returns:
            所有待发送的聚合消息
        """
        messages = []
        with self._lock:
            for key, bucket in self._buckets.items():
                if bucket.count > 0:
                    messages.append(bucket.to_summary())
            self._buckets.clear()
        return messages
    
    def get_pending_count(self) -> int:
        """获取待聚合的告警数量"""
        with self._lock:
            return sum(b.count for b in self._buckets.values())


# 全局实例
_aggregator: Optional[AlertAggregator] = None


def get_alert_aggregator() -> AlertAggregator:
    """获取全局告警聚合器"""
    global _aggregator
    if _aggregator is None:
        _aggregator = AlertAggregator()
    return _aggregator
