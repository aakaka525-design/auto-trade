# DEX HFT 核心模块实现详情

> 存档文档 - 供下游协作者参考 (更新: 2026-01-09)

## 已实现模块清单

### 1. 异常层 (`core/exceptions.py`)

新增 DEX 特定异常:

| 异常类 | 用途 | retryable |
|--------|------|-----------|
| `RPCTimeoutError` | RPC 超时 | ✓ |
| `RateLimitExceededError` | HTTP 429 | ✓ |
| `NonceConflictError` | Nonce 重复 | ✓ |
| `InsufficientLiquidityError` | 流动性不足 | ✗ |
| `OrderTimeoutError` | 订单超时 | ✓ |

---

### 2. 重试工具 (`connectors/retry.py`)

```python
# 指数退避重试
await retry_async(lambda: api_call(), config=RetryConfig(max_retries=5))

# 令牌桶限流
limiter = TokenBucketLimiter(rate=400)  # 400/秒
await limiter.acquire(weight=6)

# Nonce 管理
manager = NonceManager(initial_nonce=100)
nonce = await manager.get_next()
```

---

### 3. 执行引擎 (`engine/execution_engine.py`)

核心接口:

```python
engine = ExecutionEngine(connector, event_bus)
await engine.start()

# 提交订单
order_id = await engine.submit(signal, symbol="ETH-USDC", size=0.1)

# 等待完成
result = await engine.wait_for(order_id)
```

特性:
- 优先级队列调度
- 并发限制 (默认 5)
- 状态机跟踪 (PENDING → SUBMITTING → FILLED)

---

### 4. HFT 策略模板 (`strategies/hft_scalper.py`)

信号触发逻辑:

```python
strategy = HFTScalperStrategy(
    spread_threshold_pct=0.001,  # 0.1% 价差
    imbalance_threshold=0.3      # 30% 不平衡
)

# 订单簿驱动信号
signal = strategy.on_orderbook(orderbook)
```

---

### 5. LighterConnector 增强

重构要点:
- 集成 `TokenBucketLimiter` (Premium: 24000 权重/分钟)
- Nonce 冲突自动恢复
- WebSocket 断线自动重连 (最多 10 次)
- 健康检查接口

---

## Global ID 规范

| 类型 | 格式 | 示例 |
|------|------|------|
| 订单 | `ORD_{SIDE}_{TS}_{SEQ}` | `ORD_BUY_1704789600_1234` |
| 信号 | `SIG_{ACTION}_{TS}` | `SIG_BUY_1704789600` |

---

## 下游集成指引

1. **启动执行引擎**
   ```python
   from engine import ExecutionEngine, get_event_bus
   from connectors.lighter import LighterConnector
   
   connector = LighterConnector(config)
   await connector.connect()
   
   engine = ExecutionEngine(connector, get_event_bus())
   await engine.start()
   ```

2. **注册策略**
   ```python
   from strategies import HFTScalperStrategy
   
   strategy = HFTScalperStrategy()
   # 订阅订单簿更新
   async for ob in connector.stream_orderbook("ETH-USDC"):
       signal = strategy.on_orderbook(ob)
       if signal and signal.is_entry:
           await engine.submit(signal, symbol="ETH-USDC", size=0.1)
   ```
