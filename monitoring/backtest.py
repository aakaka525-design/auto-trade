"""
回测引擎

支持历史数据回放，验证告警策略和滑点计算逻辑。

使用方法:
```python
from monitoring.backtest import BacktestEngine, TradeEvent

engine = BacktestEngine(
    slippage_thresholds={"low": 0.5, "medium": 2.0, "high": 10.0}
)

# 加载历史数据
engine.load_trades("data/trades_2026_01.csv")

# 运行回测
results = engine.run()
print(results.summary())
```
"""
import csv
import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TradeEvent:
    """历史成交事件"""
    timestamp: datetime
    symbol: str
    market: str           # spot | futures
    side: str             # BUY | SELL
    price: float
    size: float
    is_buyer_maker: bool
    
    @property
    def value(self) -> float:
        return self.price * self.size
    
    @classmethod
    def from_csv_row(cls, row: Dict[str, str]) -> "TradeEvent":
        """从 CSV 行创建"""
        return cls(
            timestamp=datetime.fromisoformat(row["timestamp"]),
            symbol=row["symbol"],
            market=row.get("market", "spot"),
            side=row["side"],
            price=float(row["price"]),
            size=float(row["size"]),
            is_buyer_maker=row.get("is_buyer_maker", "true").lower() == "true"
        )


@dataclass
class BacktestResult:
    """回测结果"""
    total_trades: int = 0
    alerts_triggered: int = 0
    alerts_by_level: Dict[str, int] = field(default_factory=dict)
    alerts_by_symbol: Dict[str, int] = field(default_factory=dict)
    max_slippage: float = 0.0
    avg_slippage: float = 0.0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    
    def summary(self) -> str:
        """生成摘要报告"""
        duration = (self.end_time - self.start_time) if self.start_time and self.end_time else None
        
        lines = [
            "=" * 50,
            "📊 回测结果",
            "=" * 50,
            f"总成交: {self.total_trades:,}",
            f"触发告警: {self.alerts_triggered}",
            f"最大滑点: {self.max_slippage:.2f}%",
            f"平均滑点: {self.avg_slippage:.2f}%",
            "",
            "按级别统计:",
        ]
        
        for level, count in sorted(self.alerts_by_level.items()):
            lines.append(f"  {level.upper()}: {count}")
        
        lines.append("")
        lines.append("热门币种 Top 5:")
        
        top_symbols = sorted(
            self.alerts_by_symbol.items(), 
            key=lambda x: x[1], 
            reverse=True
        )[:5]
        
        for symbol, count in top_symbols:
            lines.append(f"  {symbol}: {count}")
        
        if duration:
            lines.append("")
            lines.append(f"回测时长: {duration}")
        
        lines.append("=" * 50)
        return "\n".join(lines)


class BacktestEngine:
    """
    回测引擎
    
    特性:
    - 加载历史成交数据
    - 模拟订单簿和滑点计算
    - 统计告警触发情况
    - 支持自定义策略回调
    """
    
    def __init__(
        self,
        slippage_thresholds: Dict[str, float] = None,
        min_order_value: float = 50000.0
    ):
        self.thresholds = slippage_thresholds or {
            "low": 0.5,
            "medium": 2.0,
            "high": 10.0
        }
        self.min_order_value = min_order_value
        
        self._trades: List[TradeEvent] = []
        self._on_alert: Optional[Callable] = None
    
    def load_trades(self, path: str) -> int:
        """
        加载历史成交数据
        
        CSV 格式:
        timestamp,symbol,market,side,price,size,is_buyer_maker
        
        Returns:
            加载的成交数量
        """
        file_path = Path(path)
        if not file_path.exists():
            raise FileNotFoundError(f"数据文件不存在: {path}")
        
        self._trades.clear()
        
        with open(file_path, newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    trade = TradeEvent.from_csv_row(row)
                    self._trades.append(trade)
                except (KeyError, ValueError) as e:
                    logger.warning(f"跳过无效行: {e}")
        
        # 按时间排序
        self._trades.sort(key=lambda t: t.timestamp)
        
        logger.info(f"已加载 {len(self._trades)} 条成交记录")
        return len(self._trades)
    
    def load_trades_from_list(self, trades: List[TradeEvent]):
        """从列表加载成交数据"""
        self._trades = sorted(trades, key=lambda t: t.timestamp)
    
    def on_alert(self, callback: Callable[[TradeEvent, str, float], None]):
        """
        设置告警回调
        
        Args:
            callback: 回调函数 (trade, level, slippage)
        """
        self._on_alert = callback
    
    def _get_alert_level(self, slippage: float) -> Optional[str]:
        """根据滑点获取告警级别"""
        if slippage >= self.thresholds.get("high", 10.0):
            return "high"
        elif slippage >= self.thresholds.get("medium", 2.0):
            return "medium"
        elif slippage >= self.thresholds.get("low", 0.5):
            return "low"
        return None
    
    def _simulate_slippage(self, trade: TradeEvent) -> float:
        """
        模拟滑点计算
        
        简化实现: 基于成交金额估算滑点
        实际应使用历史订单簿数据
        """
        # 简化公式: 滑点 ≈ 成交金额 / 基准流动性 * 系数
        base_liquidity = 1_000_000.0  # 假设基准流动性 $1M
        coefficient = 5.0  # 调整系数
        
        slippage = (trade.value / base_liquidity) * coefficient
        return min(slippage, 100.0)  # 最大 100%
    
    def run(self) -> BacktestResult:
        """
        运行回测
        
        Returns:
            回测结果
        """
        if not self._trades:
            logger.warning("没有加载成交数据")
            return BacktestResult()
        
        result = BacktestResult(
            start_time=self._trades[0].timestamp,
            end_time=self._trades[-1].timestamp,
            alerts_by_level={"low": 0, "medium": 0, "high": 0}
        )
        
        total_slippage = 0.0
        slippage_count = 0
        
        for trade in self._trades:
            result.total_trades += 1
            
            # 过滤小额成交
            if trade.value < self.min_order_value:
                continue
            
            # 计算滑点
            slippage = self._simulate_slippage(trade)
            
            # 获取告警级别
            level = self._get_alert_level(slippage)
            if level:
                result.alerts_triggered += 1
                result.alerts_by_level[level] = result.alerts_by_level.get(level, 0) + 1
                result.alerts_by_symbol[trade.symbol] = result.alerts_by_symbol.get(trade.symbol, 0) + 1
                result.max_slippage = max(result.max_slippage, slippage)
                
                total_slippage += slippage
                slippage_count += 1
                
                # 触发回调
                if self._on_alert:
                    self._on_alert(trade, level, slippage)
        
        if slippage_count > 0:
            result.avg_slippage = total_slippage / slippage_count
        
        return result
    
    def generate_sample_data(self, output_path: str, count: int = 1000):
        """
        生成示例数据用于测试
        
        Args:
            output_path: 输出文件路径
            count: 生成数量
        """
        import random
        
        symbols = ["ETH-USDT", "BTC-USDT", "SOL-USDT", "XRP-USDT", "DOGE-USDT"]
        markets = ["spot", "futures"]
        
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "symbol", "market", "side", "price", "size", "is_buyer_maker"])
            
            base_time = datetime.now()
            for i in range(count):
                ts = base_time.replace(second=i % 60, microsecond=i * 1000 % 1000000)
                symbol = random.choice(symbols)
                market = random.choice(markets)
                side = random.choice(["BUY", "SELL"])
                
                # 根据币种设置基准价格
                base_prices = {"ETH-USDT": 3000, "BTC-USDT": 95000, "SOL-USDT": 200, "XRP-USDT": 2.5, "DOGE-USDT": 0.3}
                price = base_prices.get(symbol, 100) * (1 + random.uniform(-0.01, 0.01))
                
                # 偶尔生成大单
                if random.random() < 0.05:
                    size = random.uniform(10000, 100000) / price  # 大单
                else:
                    size = random.uniform(100, 5000) / price  # 普通单
                
                writer.writerow([ts.isoformat(), symbol, market, side, f"{price:.2f}", f"{size:.4f}", random.choice(["true", "false"])])
        
        logger.info(f"已生成 {count} 条示例数据: {output_path}")
