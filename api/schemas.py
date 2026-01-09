"""
API 数据模型定义 (Pydantic Schemas)
"""
from enum import Enum
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, Field


class TradingStatus(str, Enum):
    """交易引擎状态"""
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"  # 触发风控暂停


class AIAnalysis(BaseModel):
    """AI 分析结果"""
    timestamp: datetime
    action: str
    confidence: float
    reason: str
    trend: Optional[str] = None
    key_signals: List[str] = []


class PositionInfo(BaseModel):
    """当前持仓信息"""
    side: Optional[str] = None  # "long" | "short" | None
    size_usdc: float = 0
    entry_price: Optional[float] = None
    unrealized_pnl_pct: float = 0


class RiskMetrics(BaseModel):
    """风控指标"""
    daily_pnl_pct: float = 0
    max_daily_loss_pct: float = 5.0
    trades_today: int = 0
    win_rate: float = 0.55


class StatusResponse(BaseModel):
    """GET /status 响应"""
    status: TradingStatus
    symbol: str
    current_price: Optional[float] = None
    position: PositionInfo
    last_analysis: Optional[AIAnalysis] = None
    risk_metrics: RiskMetrics
    uptime_seconds: int = 0
    
    class Config:
        json_schema_extra = {
            "example": {
                "status": "running",
                "symbol": "BTC-USDC",
                "current_price": 45000.0,
                "position": {
                    "side": "long",
                    "size_usdc": 500.0,
                    "entry_price": 44800.0,
                    "unrealized_pnl_pct": 0.45
                },
                "last_analysis": {
                    "timestamp": "2024-01-15T10:30:00",
                    "action": "hold",
                    "confidence": 0.72,
                    "reason": "RSI 超买，等待回调",
                    "trend": "bullish",
                    "key_signals": ["RSI > 70", "MACD 金叉"]
                },
                "risk_metrics": {
                    "daily_pnl_pct": 1.5,
                    "max_daily_loss_pct": 5.0,
                    "trades_today": 3,
                    "win_rate": 0.67
                },
                "uptime_seconds": 3600
            }
        }


class StartRequest(BaseModel):
    """POST /start 请求"""
    symbol: str = Field(default="BTC-USDC", description="交易对")
    interval_seconds: int = Field(default=300, ge=60, le=3600, description="分析间隔（秒）")
    max_position_usdc: float = Field(default=1000, ge=10, le=100000, description="最大仓位（USDC）")
    
    class Config:
        json_schema_extra = {
            "example": {
                "symbol": "BTC-USDC",
                "interval_seconds": 300,
                "max_position_usdc": 1000
            }
        }


class StartResponse(BaseModel):
    """POST /start 响应"""
    success: bool
    message: str
    config: Optional[dict] = None


class StopResponse(BaseModel):
    """POST /stop 响应"""
    success: bool
    message: str
    final_pnl_usdc: Optional[float] = None
    trades_executed: int = 0


class HealthResponse(BaseModel):
    """GET /health 响应"""
    status: str = "healthy"
    version: str = "1.0.0"
    ai_provider: str
    exchange: str
    timestamp: datetime
