"""
FastAPI 路由定义 - 后端 API 接口 (The "Face")
"""
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, HTTPException, BackgroundTasks

from api.schemas import (
    StatusResponse, StartRequest, StartResponse, StopResponse,
    HealthResponse, TradingStatus, AIAnalysis, PositionInfo, RiskMetrics
)
from config import settings
from core.ai_client import CloudAIClient
from core.prompt_builder import PromptBuilder
from core.signal_parser import SignalParser, TradingSignal
from core.exceptions import SignalParseError, AIProviderError
from trading.data_fetcher import DataFetcher
from trading.risk_manager import RiskManager, PositionSizing
from trading.order_executor import LighterExecutor


router = APIRouter(prefix="/api/v1", tags=["Trading"])


class TradingEngine:
    """交易引擎状态管理（单例）"""
    
    def __init__(self):
        self.status = TradingStatus.STOPPED
        self.start_time: Optional[datetime] = None
        self.last_analysis: Optional[AIAnalysis] = None
        self.position = PositionInfo()
        self.current_price: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._interval: int = 300
        
        # 交易统计
        self.trades_today: int = 0
        self.wins_today: int = 0
        
        # 组件（延迟初始化）
        self._ai_client: Optional[CloudAIClient] = None
        self._prompt_builder: Optional[PromptBuilder] = None
        self._signal_parser: Optional[SignalParser] = None
        self._risk_manager: Optional[RiskManager] = None
        self._data_fetcher: Optional[DataFetcher] = None
        self._executor: Optional[LighterExecutor] = None
    
    @property
    def ai_client(self) -> CloudAIClient:
        if self._ai_client is None:
            self._ai_client = CloudAIClient()
        return self._ai_client
    
    @property
    def prompt_builder(self) -> PromptBuilder:
        if self._prompt_builder is None:
            self._prompt_builder = PromptBuilder()
        return self._prompt_builder
    
    @property
    def signal_parser(self) -> SignalParser:
        if self._signal_parser is None:
            self._signal_parser = SignalParser()
        return self._signal_parser
    
    @property
    def risk_manager(self) -> RiskManager:
        if self._risk_manager is None:
            self._risk_manager = RiskManager()
        return self._risk_manager
    
    @property
    def data_fetcher(self) -> DataFetcher:
        if self._data_fetcher is None:
            self._data_fetcher = DataFetcher()
        return self._data_fetcher
    
    @property
    def executor(self) -> LighterExecutor:
        if self._executor is None:
            self._executor = LighterExecutor()
        return self._executor
    
    def get_win_rate(self) -> float:
        """计算当日胜率"""
        if self.trades_today == 0:
            return 0.55  # 默认胜率
        return self.wins_today / self.trades_today


# 全局引擎实例
engine = TradingEngine()


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        ai_provider=settings.AI_PROVIDER,
        exchange="Lighter",
        timestamp=datetime.now()
    )


@router.get("/status", response_model=StatusResponse)
async def get_status():
    """
    获取当前交易状态
    
    返回：
    - 当前运行状态
    - 持仓信息
    - 最近一次 AI 分析结果
    - 风控指标
    """
    uptime = 0
    if engine.start_time:
        uptime = int((datetime.now() - engine.start_time).total_seconds())
    
    risk_metrics = RiskMetrics(
        daily_pnl_pct=engine.risk_manager.daily_pnl_pct,
        max_daily_loss_pct=engine.risk_manager.max_daily_loss_pct,
        trades_today=engine.trades_today,
        win_rate=engine.get_win_rate()
    )
    
    return StatusResponse(
        status=engine.status,
        symbol=settings.TRADING_SYMBOL,
        current_price=engine.current_price,
        position=engine.position,
        last_analysis=engine.last_analysis,
        risk_metrics=risk_metrics,
        uptime_seconds=uptime
    )


@router.post("/start", response_model=StartResponse)
async def start_trading(request: StartRequest, background_tasks: BackgroundTasks):
    """
    启动自动交易
    
    参数：
    - symbol: 交易对
    - interval_seconds: 分析间隔（秒）
    - max_position_usdc: 最大仓位（USDC）
    """
    if engine.status == TradingStatus.RUNNING:
        raise HTTPException(status_code=400, detail="交易引擎已在运行")
    
    # 验证 API Key 配置
    if not settings.AI_API_KEY:
        raise HTTPException(status_code=400, detail="未配置 AI_API_KEY 环境变量")
    
    engine.status = TradingStatus.RUNNING
    engine.start_time = datetime.now()
    engine._interval = request.interval_seconds
    engine.risk_manager.max_position_usdc = request.max_position_usdc
    
    # 重置当日统计
    engine.trades_today = 0
    engine.wins_today = 0
    engine.risk_manager.reset_daily_stats()
    
    # 启动后台交易循环
    background_tasks.add_task(trading_loop, request.interval_seconds)
    
    return StartResponse(
        success=True,
        message=f"交易引擎已启动，分析间隔 {request.interval_seconds} 秒",
        config={
            "symbol": request.symbol,
            "interval_seconds": request.interval_seconds,
            "max_position_usdc": request.max_position_usdc
        }
    )


@router.post("/stop", response_model=StopResponse)
async def stop_trading():
    """
    停止自动交易
    
    返回：
    - 最终盈亏统计
    """
    if engine.status == TradingStatus.STOPPED:
        raise HTTPException(status_code=400, detail="交易引擎未运行")
    
    engine.status = TradingStatus.STOPPED
    
    # 取消后台任务
    if engine._task and not engine._task.done():
        engine._task.cancel()
        try:
            await engine._task
        except asyncio.CancelledError:
            pass
    
    # 取消所有活跃订单
    await engine.executor.cancel_all_orders()
    
    final_pnl = engine.risk_manager.daily_loss_usdc
    
    return StopResponse(
        success=True,
        message="交易引擎已停止",
        final_pnl_usdc=final_pnl,
        trades_executed=engine.trades_today
    )


@router.post("/analyze")
async def manual_analyze():
    """
    手动触发一次 AI 分析（不执行交易）
    
    用于测试 AI Prompt 和信号解析
    """
    try:
        # 获取市场数据
        market_data = await engine.data_fetcher.fetch_market_data()
        engine.current_price = market_data.indicators.current_price
        
        # 构建 Prompt
        system_prompt = engine.prompt_builder.get_system_prompt()
        user_prompt = engine.prompt_builder.build_user_prompt(market_data.to_dict())
        
        # 调用 AI 分析
        raw_response = await engine.ai_client.analyze(system_prompt, user_prompt)
        
        # 解析信号
        signal = engine.signal_parser.parse(raw_response)
        
        # 风控计算
        sizing = engine.risk_manager.calculate_position(
            signal=signal,
            current_price=market_data.indicators.current_price,
            available_balance=market_data.available_balance,
            win_rate=engine.get_win_rate()
        )
        
        return {
            "success": True,
            "signal": {
                "action": signal.action,
                "confidence": signal.confidence,
                "reason": signal.reason,
                "analysis": signal.analysis
            },
            "position_sizing": {
                "should_trade": sizing.should_trade,
                "position_size_usdc": sizing.position_size_usdc,
                "stop_loss_price": sizing.stop_loss_price,
                "take_profit_price": sizing.take_profit_price,
                "rejection_reason": sizing.rejection_reason
            },
            "market_data": {
                "current_price": market_data.indicators.current_price,
                "rsi": market_data.indicators.rsi_value,
                "macd": market_data.indicators.macd_line
            },
            "raw_response": raw_response
        }
        
    except SignalParseError as e:
        return {"success": False, "error": f"信号解析失败: {e}"}
    except AIProviderError as e:
        return {"success": False, "error": f"AI 调用失败: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def trading_loop(interval: int):
    """
    后台交易循环
    """
    print(f"🔄 交易循环启动，间隔 {interval} 秒")
    
    while engine.status == TradingStatus.RUNNING:
        try:
            print(f"\n{'='*50}")
            print(f"⏰ {datetime.now().isoformat()} - 开始分析")
            
            # 1. 获取市场数据
            market_data = await engine.data_fetcher.fetch_market_data()
            engine.current_price = market_data.indicators.current_price
            print(f"📊 当前价格: {engine.current_price:.2f}")
            
            # 2. 构建 Prompt
            system_prompt = engine.prompt_builder.get_system_prompt()
            user_prompt = engine.prompt_builder.build_user_prompt(market_data.to_dict())
            
            # 3. 调用 AI 分析
            print("🧠 调用云端 AI 分析...")
            raw_response = await engine.ai_client.analyze(system_prompt, user_prompt)
            
            # 4. 解析信号
            signal = engine.signal_parser.parse(raw_response)
            print(f"📈 AI 信号: {signal.action.upper()} (置信度: {signal.confidence:.2f})")
            print(f"💬 理由: {signal.reason}")
            
            # 5. 更新最近分析
            engine.last_analysis = AIAnalysis(
                timestamp=datetime.now(),
                action=signal.action,
                confidence=signal.confidence,
                reason=signal.reason,
                trend=signal.analysis.get("trend"),
                key_signals=signal.analysis.get("key_signals", [])
            )
            
            # 6. 风控计算
            sizing = engine.risk_manager.calculate_position(
                signal=signal,
                current_price=market_data.indicators.current_price,
                available_balance=market_data.available_balance,
                win_rate=engine.get_win_rate()
            )
            
            # 7. 执行交易
            if sizing.should_trade:
                print(f"✅ 风控通过，执行交易...")
                # is_ask=True 表示卖出（做空），is_ask=False 表示买入（做多）
                is_ask = (signal.action == "sell")
                
                result = await engine.executor.execute_order(
                    is_ask=is_ask,
                    sizing=sizing,
                    current_price=market_data.indicators.current_price
                )
                
                if result.success:
                    engine.trades_today += 1
                    engine.position = PositionInfo(
                        side="long" if signal.action == "buy" else "short",
                        size_usdc=sizing.position_size_usdc,
                        entry_price=market_data.indicators.current_price,
                        unrealized_pnl_pct=0
                    )
                    print(f"🎯 订单成功: #{result.order_index}")
                else:
                    print(f"❌ 订单失败: {result.error_message}")
            else:
                print(f"⏸️ 跳过交易: {sizing.rejection_reason}")
        
        except SignalParseError as e:
            print(f"⚠️ 信号解析错误: {e}")
        except AIProviderError as e:
            print(f"⚠️ AI 调用错误: {e}")
        except Exception as e:
            print(f"❌ 交易循环错误: {e}")
        
        # 等待下一次循环
        print(f"💤 等待 {interval} 秒...")
        await asyncio.sleep(interval)
    
    print("🛑 交易循环已停止")
