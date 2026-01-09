"""
交易机器人主循环

整合 Connector, Strategy, ExecutionEngine 的主交易引擎。
"""
import asyncio
import logging
from datetime import datetime
from typing import Optional, List

from config import settings
from connectors import LighterConnector, Candlestick
from strategies import BaseStrategy, MomentumStrategy, TrendFollowerStrategy, Signal
from engine import EventBus, ExecutionEngine, Event, EventType, get_event_bus
from risk import RiskManager, RiskConfig, PositionSizer

logger = logging.getLogger(__name__)


class TradingBot:
    """
    HFT 交易机器人
    
    整合所有模块的主控制器:
    - Connector: 交易所连接
    - Strategies: 策略信号生成
    - Engine: 订单执行
    - Risk: 风控检查
    
    使用示例:
    ```python
    bot = TradingBot(
        symbol="ETH-USDC",
        strategies=[
            MomentumStrategy(roc_threshold=0.002),
            TrendFollowerStrategy(fast_period=9)
        ]
    )
    
    await bot.start()
    ```
    """
    
    def __init__(
        self,
        symbol: str = "ETH-USDC",
        strategies: Optional[List[BaseStrategy]] = None,
        interval_seconds: float = 5.0,
    ):
        self.symbol = symbol
        self.interval = interval_seconds
        
        # 组件
        self.connector: Optional[LighterConnector] = None
        self.strategies: List[BaseStrategy] = strategies or []
        self.event_bus: EventBus = get_event_bus()
        self.engine: Optional[ExecutionEngine] = None
        self.risk_manager: Optional[RiskManager] = None
        self.position_sizer: Optional[PositionSizer] = None
        
        # 状态
        self._running = False
        self._last_price: float = 0.0
        self._loop_count = 0
        
        # K 线缓存
        self._candles: List[Candlestick] = []
    
    async def initialize(self) -> bool:
        """初始化所有组件"""
        logger.info("🚀 初始化交易机器人...")
        
        # 1. 连接器
        self.connector = LighterConnector({
            "base_url": settings.LIGHTER_BASE_URL,
            "account_index": settings.LIGHTER_ACCOUNT_INDEX,
            "api_key_index": settings.LIGHTER_API_KEY_INDEX,
            "api_private_key": settings.LIGHTER_API_PRIVATE_KEY,
            "http_proxy": settings.HTTP_PROXY,
            "https_proxy": settings.HTTPS_PROXY,
        })
        
        connected = await self.connector.connect()
        if not connected:
            logger.error("❌ 连接器初始化失败")
            return False
        
        # 2. 执行引擎
        self.engine = ExecutionEngine(
            connector=self.connector,
            event_bus=self.event_bus,
            max_concurrent=3,
        )
        await self.engine.start()
        
        # 3. 风控 (HardCheck)
        risk_config = RiskConfig(
            max_position_size={self.symbol: settings.MAX_POSITION_SIZE_USDC / 1000},  # 转换为币数
            max_daily_loss=settings.MAX_DAILY_LOSS_PCT * 100,  # 转换为 USDC
            max_single_order_size={self.symbol: settings.MAX_POSITION_SIZE_USDC / 1000},
        )
        self.risk_manager = RiskManager(risk_config)
        
        # 4. 仓位计算器 (Kelly Criterion)
        self.position_sizer = PositionSizer(
            max_position_usdc=settings.MAX_POSITION_SIZE_USDC,
            max_loss_per_trade_pct=settings.MAX_LOSS_PER_TRADE_PCT,
            max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT,
        )
        
        # 4. 默认策略
        if not self.strategies:
            self.strategies = [
                MomentumStrategy(
                    roc_period=10,
                    roc_threshold=0.002,
                    min_signal_interval_sec=10.0
                ),
                TrendFollowerStrategy(
                    fast_period=9,
                    slow_period=21
                )
            ]
        
        # 5. 订阅事件
        self.event_bus.subscribe(EventType.ORDER_FILLED, self._on_order_filled)
        
        logger.info("✅ 交易机器人初始化完成")
        return True
    
    async def start(self) -> None:
        """启动交易循环"""
        if not await self.initialize():
            return
        
        self._running = True
        logger.info(f"📈 开始交易: {self.symbol} @ {self.interval}s 间隔")
        
        # 发布启动事件
        await self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_START,
            data={"symbol": self.symbol},
            source="trading_bot"
        ))
        
        try:
            while self._running:
                await self._trading_loop()
                await asyncio.sleep(self.interval)
                
        except asyncio.CancelledError:
            logger.info("交易循环被取消")
        except Exception as e:
            logger.exception(f"交易循环异常: {e}")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """停止交易"""
        self._running = False
        
        # 取消所有订单
        if self.connector:
            await self.connector.cancel_all_orders()
        
        # 停止引擎
        if self.engine:
            await self.engine.stop()
        
        # 断开连接
        if self.connector:
            await self.connector.disconnect()
        
        await self.event_bus.publish(Event(
            event_type=EventType.SYSTEM_STOP,
            data={},
            source="trading_bot"
        ))
        
        logger.info("⏹️ 交易机器人已停止")
    
    async def _trading_loop(self) -> None:
        """单次交易循环"""
        self._loop_count += 1
        
        try:
            # 1. 获取最新价格 (使用 recent_trades，不会 403)
            price = await self.connector.get_ticker_price(self.symbol)
            if price <= 0:
                return
            
            self._last_price = price
            
            # 2. 构造模拟 K 线 (跳过 get_candlesticks 因为 403)
            # 使用价格序列模拟 K 线数据
            now_ts = int(datetime.now().timestamp())
            candle = Candlestick(
                timestamp=now_ts,
                open=price,
                high=price * 1.001,
                low=price * 0.999,
                close=price,
                volume=1.0  # 模拟
            )
            self._candles.append(candle)
            
            # 保留最近 100 根
            if len(self._candles) > 100:
                self._candles = self._candles[-100:]
            
            # 3. 运行策略 (需要足够数据)
            if len(self._candles) >= 30:
                for strategy in self.strategies:
                    if not strategy.is_enabled:
                        continue
                    
                    # 喂最新 K 线
                    signal = strategy.on_candle(candle)
                    
                    if signal and signal.is_entry:
                        await self._handle_signal(signal, strategy.name)
            
            # 4. 发布价格事件
            await self.event_bus.publish(Event(
                event_type=EventType.PRICE_UPDATE,
                data={"symbol": self.symbol, "price": price},
                source="trading_bot"
            ))
            
            # 5. 日志
            if self._loop_count % 12 == 0:  # 每分钟
                logger.info(f"💰 {self.symbol}: ${price:.2f} (数据点: {len(self._candles)})")
                
        except Exception as e:
            logger.error(f"交易循环错误: {e}")
    
    async def _handle_signal(self, signal: Signal, source: str) -> None:
        """处理策略信号"""
        logger.info(
            f"📊 [{source}] {signal.action.value.upper()} @ ${signal.price:.2f} "
            f"(置信度: {signal.confidence:.2%})"
        )
        
        # 发布信号事件
        await self.event_bus.publish(Event(
            event_type=EventType.SIGNAL_GENERATED,
            data={
                "action": signal.action.value,
                "price": signal.price,
                "confidence": signal.confidence,
                "source": source,
            },
            source=source
        ))
        
        # 风控检查
        if signal.confidence < settings.MIN_CONFIDENCE_THRESHOLD:
            logger.debug(f"信号置信度不足: {signal.confidence:.2%}")
            return
        
        # 计算仓位
        position_size = self._calculate_position_size(signal)
        
        if position_size <= 0:
            return
        
        # 提交订单
        try:
            order_id = await self.engine.submit(
                signal=signal,
                symbol=self.symbol,
                size=position_size,
                price=signal.price,
            )
            logger.info(f"📤 订单已提交: {order_id}")
            
        except Exception as e:
            logger.error(f"订单提交失败: {e}")
    
    def _calculate_position_size(self, signal: Signal) -> float:
        """计算仓位大小 (使用 PositionSizer)"""
        if self.position_sizer:
            return self.position_sizer.calculate_position(
                entry_price=signal.price,
                stop_loss=signal.stop_loss or signal.price * 0.99,
                confidence=signal.confidence,
            ).position_usdc / signal.price
        
        # 默认: 0.01 ETH
        return 0.01
    
    async def _on_order_filled(self, event: Event) -> None:
        """订单成交回调"""
        logger.info(f"✅ 订单成交: {event.data}")
    
    # ==================== 状态查询 ====================
    
    def get_status(self) -> dict:
        """获取机器人状态"""
        return {
            "running": self._running,
            "symbol": self.symbol,
            "last_price": self._last_price,
            "loop_count": self._loop_count,
            "strategies": [s.get_stats() for s in self.strategies],
            "engine": self.engine.get_stats() if self.engine else None,
        }


# ==================== 便捷启动 ====================

async def run_bot(symbol: str = "ETH-USDC"):
    """快速启动交易机器人"""
    bot = TradingBot(symbol=symbol)
    await bot.start()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    asyncio.run(run_bot())
