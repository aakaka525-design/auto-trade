# 实现细节文档

> **更新日期**: 2026-01-11  
> **版本**: 2.0

---

## 一、多交易所连接器 (Connectors)

### 1.1 架构概览

```
connectors/
├── base.py                  # BaseConnector 抽象基类
├── factory.py               # ConnectorFactory 工厂模式
├── lighter/
│   ├── client.py            # LighterConnector
│   ├── ws_orderbook.py      # WebSocket 订单簿
│   └── account_ws.py        # 账户数据流
└── binance/
    ├── client.py            # BinanceConnector
    ├── auth.py              # HMAC-SHA256 签名
    └── ws_streams.py        # WebSocket 数据流
```

### 1.2 BinanceConnector 功能

| 功能 | 方法 | 说明 |
|------|------|------|
| 连接管理 | `connect()`, `disconnect()` | 自动时间同步 |
| 订单簿 | `get_orderbook()` | REST API 快照 |
| K 线 | `get_candlesticks()` | 支持多时间周期 |
| 最新价 | `get_ticker_price()` | 实时价格 |
| 账户 | `get_account()` | USDT 余额 |
| 下单 | `create_order()` | 限价/市价/止损 |
| 撤单 | `cancel_order()`, `cancel_all_orders()` | 单个/批量 |
| **WS 订单簿** | `stream_orderbook()` | 实时深度流 |
| **WS 成交** | `stream_trades()` | 聚合成交流 |
| **WS 用户数据** | `get_user_data_stream()` | 订单状态更新 |

### 1.3 认证和限流

```python
# 签名 (auth.py)
class BinanceAuth:
    def sign(params) -> str          # HMAC-SHA256
    def sign_params(params) -> dict  # 添加时间戳和签名
    async def sync_server_time()     # 服务器时间同步

# 限流 (client.py)
self._rate_limiter = TokenBucketLimiter(
    rate=20,      # 每秒 20 权重
    capacity=1200 # Binance: 1200/分钟
)
```

---

## 二、配置管理

### 2.1 多交易所配置

```bash
# 交易执行
ACTIVE_EXCHANGE=lighter          # lighter | binance

# 监控 (支持多选)
MONITOR_EXCHANGES=lighter,binance

# Binance 配置
BINANCE_API_KEY=xxx
BINANCE_API_SECRET=xxx
BINANCE_TESTNET=false
```

### 2.2 工厂模式使用

```python
from connectors import ConnectorFactory

# 动态创建连接器
lighter = ConnectorFactory.create("lighter", {...})
binance = ConnectorFactory.create("binance", {...})

# 获取可用连接器
print(ConnectorFactory.available())  # ['lighter', 'binance']
```

---

## 三、WebSocket 数据流

### 3.1 Binance WebSocket (ws_streams.py)

```python
# 订单簿流
async for orderbook in connector.stream_orderbook("ETH-USDT"):
    print(f"Best Bid: {orderbook.best_bid}")

# 成交流
async for trade in connector.stream_trades("BTC-USDT"):
    print(f"{trade.side}: {trade.price} x {trade.size}")

# 用户数据流 (订单更新)
user_stream = await connector.get_user_data_stream()
await user_stream.start()

async for update in user_stream.stream_order_updates():
    print(f"订单 {update.order_id}: {update.status}")
```

### 3.2 支持的流类型

| 流类型 | Binance | Lighter |
|--------|---------|---------|
| 订单簿快照 | `@depth5/10/20` | ✅ |
| 订单簿增量 | `@depth@100ms` | ✅ |
| 成交流 | `@aggTrade` | ✅ |
| 用户数据 | listenKey | ✅ |

---

## 四、错误处理

### 4.1 异常层次

```
TradingError (基类)
├── ExchangeConnectionError
│   ├── RPCTimeoutError
│   ├── RateLimitExceededError
│   └── WebSocketDisconnectedError
└── OrderExecutionError
    ├── NonceConflictError
    ├── InsufficientBalanceError
    └── OrderRejectedError
```

### 4.2 Binance 错误码映射

| 错误码 | 含义 | 处理 |
|--------|------|------|
| `-1000` | 未知错误 | 重试 |
| `-1015` | 限流 | 等待 60s |
| `-1021` | 时间戳问题 | 重新同步 |
| `-2010` | 余额不足 | 拒绝 |
| `-2011` | 未知订单 | 忽略 |

---

## 五、测试覆盖

```bash
pytest tests/ -v
# 10/10 通过
```

| 测试模块 | 覆盖 |
|----------|------|
| `test_risk_manager.py` | RiskManager 风控检查 |
| `test_engine_integration.py` | ExecutionEngine 集成 |
| `test_lighter_api.py` | Lighter API 连接 |
