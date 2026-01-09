"""
Cloud AI Trading System - FastAPI 应用入口
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import router
from config import settings


def setup_logging():
    """配置日志系统 - 同时输出到控制台和文件"""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    
    # 创建根日志器
    logger = logging.getLogger()
    logger.setLevel(log_level)
    
    # 清除已有处理器
    logger.handlers.clear()
    
    # 格式器 - 不记录敏感信息
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件处理器
    if settings.LOG_FILE:
        file_handler = logging.FileHandler(settings.LOG_FILE, encoding='utf-8')
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


# 初始化日志
setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    print("=" * 60)
    print("🚀 Cloud AI Trading System Starting...")
    print("=" * 60)
    print(f"📡 AI Provider: {settings.AI_PROVIDER}")
    print(f"🤖 AI Model: {settings.AI_MODEL}")
    print(f"💱 Exchange: Lighter ({settings.LIGHTER_BASE_URL})")
    print(f"📈 Symbol: {settings.TRADING_SYMBOL}")
    print(f"⏱️ Analysis Interval: {settings.ANALYSIS_INTERVAL_SECONDS}s")
    print("=" * 60)
    
    if not settings.AI_API_KEY:
        print("⚠️  警告: AI_API_KEY 未配置！")
    if not settings.LIGHTER_API_PRIVATE_KEY:
        print("⚠️  警告: LIGHTER_API_PRIVATE_KEY 未配置！")
    
    print("\n📖 API 文档: http://localhost:8000/docs")
    print("📊 状态接口: http://localhost:8000/api/v1/status")
    print("\n")
    
    yield
    
    # 关闭时
    print("\n👋 Cloud AI Trading System Shutting Down...")


app = FastAPI(
    title="Cloud AI Trading System",
    description="""
## 云端 AI 驱动的加密货币量化交易系统

### 核心功能
- 🧠 **AI 决策引擎** - 调用云端大模型进行市场分析
- 📊 **技术指标分析** - RSI, MACD, EMA, 布林带等
- ⚖️ **凯利公式风控** - 科学仓位管理
- 🔗 **Lighter 集成** - 去中心化交易所订单执行

### API 接口
- `GET /api/v1/status` - 获取当前状态
- `POST /api/v1/start` - 启动自动交易
- `POST /api/v1/stop` - 停止自动交易
- `POST /api/v1/analyze` - 手动触发分析（不交易）
    """,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan
)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应限制来源
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(router)


# 根路径重定向到文档
@app.get("/", include_in_schema=False)
async def root():
    """重定向到 API 文档"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/docs")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
