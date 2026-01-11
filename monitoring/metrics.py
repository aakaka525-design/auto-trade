"""
Prometheus 指标导出模块

提供监控指标采集和 HTTP 暴露端点。

使用方法:
1. 在监控脚本中初始化:
   from monitoring.metrics import init_metrics, ALERTS_TOTAL, TRADES_PROCESSED
   init_metrics(port=9090)

2. 更新指标:
   ALERTS_TOTAL.labels(level="high", market="spot").inc()
   TRADES_PROCESSED.inc()

3. 访问 http://localhost:9090/metrics 查看指标
"""
import logging
from typing import Optional

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server, REGISTRY
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

logger = logging.getLogger(__name__)


# ==================== 指标定义 ====================

if PROMETHEUS_AVAILABLE:
    # 告警计数器
    ALERTS_TOTAL = Counter(
        'binance_alerts_total',
        'Total number of alerts triggered',
        ['level', 'market', 'alert_type']
    )
    
    # 成交处理计数器
    TRADES_PROCESSED = Counter(
        'binance_trades_processed_total',
        'Total number of trades processed',
        ['market']
    )
    
    # 活跃连接数
    ACTIVE_CONNECTIONS = Gauge(
        'binance_active_connections',
        'Number of active WebSocket connections',
        ['market']
    )
    
    # 成交处理速率
    TRADES_PER_SECOND = Gauge(
        'binance_trades_per_second',
        'Current trades processing rate per second'
    )
    
    # 滑点分布
    SLIPPAGE_HISTOGRAM = Histogram(
        'binance_slippage_percent',
        'Slippage distribution in percent',
        ['market'],
        buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0]
    )
    
    # 订单簿档位数
    ORDERBOOK_LEVELS = Gauge(
        'binance_orderbook_levels',
        'Number of orderbook levels cached',
        ['market', 'side']
    )
else:
    # Fallback: 使用空操作类
    class DummyMetric:
        def labels(self, **kwargs): return self
        def inc(self, amount=1): pass
        def dec(self, amount=1): pass
        def set(self, value): pass
        def observe(self, value): pass
    
    ALERTS_TOTAL = DummyMetric()
    TRADES_PROCESSED = DummyMetric()
    ACTIVE_CONNECTIONS = DummyMetric()
    TRADES_PER_SECOND = DummyMetric()
    SLIPPAGE_HISTOGRAM = DummyMetric()
    ORDERBOOK_LEVELS = DummyMetric()


# ==================== 初始化函数 ====================

_metrics_server_started = False


def init_metrics(port: int = 9090) -> bool:
    """
    初始化 Prometheus 指标服务器
    
    Args:
        port: HTTP 端口
        
    Returns:
        是否成功启动
    """
    global _metrics_server_started
    
    if not PROMETHEUS_AVAILABLE:
        logger.warning("prometheus_client 未安装，指标功能禁用")
        logger.warning("安装命令: pip install prometheus-client")
        return False
    
    if _metrics_server_started:
        logger.debug("Prometheus 服务器已启动")
        return True
    
    try:
        start_http_server(port)
        _metrics_server_started = True
        logger.info(f"✅ Prometheus 指标服务启动: http://localhost:{port}/metrics")
        return True
    except Exception as e:
        logger.error(f"Prometheus 服务启动失败: {e}")
        return False


def update_connection_count(market: str, count: int):
    """更新连接数"""
    ACTIVE_CONNECTIONS.labels(market=market).set(count)


def record_trade(market: str):
    """记录一次成交处理"""
    TRADES_PROCESSED.labels(market=market).inc()


def record_alert(level: str, market: str, alert_type: str):
    """记录一次告警"""
    ALERTS_TOTAL.labels(level=level, market=market, alert_type=alert_type).inc()


def record_slippage(market: str, slippage: float):
    """记录滑点"""
    SLIPPAGE_HISTOGRAM.labels(market=market).observe(abs(slippage))


def update_trades_rate(rate: float):
    """更新成交速率"""
    TRADES_PER_SECOND.set(rate)
