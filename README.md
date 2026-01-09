# Cloud AI Trading System

云端 AI 驱动的加密货币量化交易系统后端

## 功能特性

- 🧠 **云端 AI 决策** - 支持 OpenAI GPT-4 / Anthropic Claude 等大模型
- 📊 **技术指标分析** - RSI, MACD, EMA, 布林带等
- ⚖️ **凯利公式风控** - 科学仓位管理，半凯利策略
- 🛡️ **硬止损保护** - 单笔最大亏损 2%，日亏损上限 5%
- 🔗 **Lighter 交易所** - 深度集成 Lighter DEX API

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 API Keys
```

### 3. 启动服务

```bash
python main.py
```

访问 http://localhost:8000/docs 查看 API 文档

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v1/status` | 获取当前状态 + AI 分析 |
| POST | `/api/v1/start` | 启动自动交易 |
| POST | `/api/v1/stop` | 停止自动交易 |

## 项目结构

```
auto_trade/
├── main.py                 # FastAPI 入口
├── config.py               # 配置管理
├── requirements.txt
├── .env.example
├── core/                   # 核心 AI 模块
│   ├── ai_client.py        # 云端 AI 调用
│   ├── prompt_builder.py   # Prompt 构造
│   ├── signal_parser.py    # 信号解析
│   └── exceptions.py
├── trading/                # 交易模块
│   ├── data_fetcher.py     # 行情获取
│   ├── indicators.py       # 技术指标
│   ├── risk_manager.py     # 风控引擎
│   └── order_executor.py   # 订单执行
└── api/                    # API 层
    ├── routes.py
    └── schemas.py
```

## 安全提醒

⚠️ **永远不要将 API Keys 提交到版本控制！**

- 所有敏感配置通过 `.env` 管理
- `.env` 已添加到 `.gitignore`

## License

MIT
