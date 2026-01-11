# 多交易所量化交易系统

支持 **Lighter DEX** 和 **Binance Spot** 的量化交易和市场监控系统

## 支持的交易所

| 交易所 | 类型 | 功能 |
|--------|------|------|
| Lighter | DEX | 行情/交易/WebSocket |
| Binance | CEX | 行情/交易/WebSocket |

## 功能模块

### 🔍 多市场监控
实时监控多个交易对的大单和价格异常

```bash
python scripts/run_multi_market_monitor.py
```

**特性：**
- 支持 Lighter + Binance 同时监控
- 分级大单阈值（主流币/其他币）
- 价格拉升/暴跌警报
- Telegram 实时推送

### ⚙️ 配置

```bash
cp .env.example .env
# 编辑 .env 填入配置
```

**关键配置项：**

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `ACTIVE_EXCHANGE` | 交易执行交易所 | lighter |
| `MONITOR_EXCHANGES` | 监控交易所列表 | lighter,binance |
| `LARGE_ORDER_MIN_VALUE_MAJOR` | 主流币大单阈值 | $1,000,000 |
| `LARGE_ORDER_MIN_VALUE_OTHER` | 其他币大单阈值 | $100,000 |
| `PRICE_PUMP_THRESHOLD` | 拉升警报阈值 (%) | 0.5 |
| `BINANCE_API_KEY` | Binance API Key | - |
| `BINANCE_API_SECRET` | Binance Secret | - |

### 📁 项目结构

```
auto_trade/
├── connectors/
│   ├── base.py                       # BaseConnector 抽象基类
│   ├── factory.py                    # ConnectorFactory 工厂模式
│   ├── lighter/                      # Lighter DEX 连接器
│   │   ├── client.py
│   │   ├── ws_orderbook.py
│   │   └── account_ws.py
│   └── binance/                      # Binance Spot 连接器
│       ├── client.py
│       ├── auth.py
│       └── ws_streams.py
├── scripts/
│   └── run_multi_market_monitor.py   # 多市场监控脚本
├── monitoring/
│   ├── large_order_monitor.py        # 大单检测
│   └── price_monitor.py              # 价格异常检测
├── engine/
│   └── execution_engine.py           # 订单执行引擎
├── strategies/
│   ├── base.py                       # 策略基类
│   └── hft_scalper.py                # HFT 剥头皮策略
├── risk/
│   └── manager.py                    # 风险管理
├── config.py                         # 配置管理
└── main.py                           # API 服务入口
```

## 安装

```bash
pip install -r requirements.txt
```

## 使用示例

### 启动市场监控

```bash
# 默认监控 13 个主流币
python scripts/run_multi_market_monitor.py

# 监控所有市场
MONITOR_MARKETS=all python scripts/run_multi_market_monitor.py

# 只监控永续合约
MONITOR_MARKETS=perp python scripts/run_multi_market_monitor.py
```

### 启动 API 服务

```bash
python main.py
# 访问 http://localhost:8000/docs
```

## 安全提醒

⚠️ **API Keys 不要提交到版本控制！**

- `.env` 已添加到 `.gitignore`
- 使用 `.env.example` 作为配置模板

## License

MIT
