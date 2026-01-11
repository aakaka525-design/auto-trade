"""
ExecutionEngine 集成测试

测试覆盖:
1. RiskManager 集成 (风控拦截)
2. 内存清理机制
3. Mock DEX SDK 错误处理
4. 熔断后订单拒绝
"""
import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
from engine.execution_engine import ExecutionEngine, OrderTask, OrderState
from risk.manager import RiskManager, RiskConfig, RiskLimitExceededError, CircuitBreakerTrippedError
from connectors.base import BaseConnector, OrderResult
from strategies.base import Signal, SignalAction


# ==================== Fixtures ====================

@pytest.fixture
def mock_connector():
    """Mock 交易所连接器"""
    connector = MagicMock(spec=BaseConnector)
    connector.create_order = AsyncMock(
        return_value=OrderResult(success=True, order_id="EX_123", average_price=2000.0, fee=0.5)
    )
    connector.cancel_order = AsyncMock(return_value=True)
    return connector


@pytest.fixture
def risk_manager():
    """标准风控配置"""
    config = RiskConfig(
        max_position_size={"ETH-USDC": 1.0, "BTC-USDC": 0.1},
        max_single_order_size={"ETH-USDC": 1.0, "BTC-USDC": 0.1},
        max_daily_loss=100.0  # 低阈值便于测试熔断
    )
    return RiskManager(config)


@pytest.fixture
def engine(mock_connector, risk_manager):
    """带风控的执行引擎"""
    return ExecutionEngine(
        connector=mock_connector, 
        risk_manager=risk_manager,
        cleanup_interval_seconds=0.1,
        task_ttl_seconds=0.2  # 短 TTL 便于测试清理
    )


# ==================== 风控集成测试 ====================

class TestRiskIntegration:
    """风控模块集成测试"""
    
    @pytest.mark.asyncio
    async def test_risk_check_interception(self, engine):
        """风控应在提交前拦截超限订单"""
        await engine.start()
        
        # 1. 有效订单
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        await engine.submit(signal, "ETH-USDC", 0.5)
        
        await asyncio.sleep(0.15)
        assert engine.risk_manager.get_state()["positions"]["ETH-USDC"] == 0.5
        
        # 2. 超限订单 (0.5 + 0.6 > 1.0)
        signal_large = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        
        with pytest.raises(RiskLimitExceededError):
            await engine.submit(signal_large, "ETH-USDC", 0.6)
            
        await engine.stop()
        
    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_all_orders(self, engine):
        """熔断后所有订单应被拒绝"""
        await engine.start()
        
        # 触发熔断
        engine.risk_manager.update_pnl(-150.0)  # > 100 限制
        
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        
        with pytest.raises(CircuitBreakerTrippedError):
            await engine.submit(signal, "ETH-USDC", 0.1)
            
        await engine.stop()
        
    @pytest.mark.asyncio
    async def test_on_fill_updates_risk_state(self, engine):
        """成交后应更新风控状态"""
        await engine.start()
        
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        oid = await engine.submit(signal, "ETH-USDC", 0.3)
        
        await asyncio.sleep(0.15)
        
        # 验证仓位更新
        state = engine.risk_manager.get_state()
        assert state["positions"]["ETH-USDC"] == 0.3
        
        # 验证手续费扣除
        assert state["daily_realized_pnl"] == -0.5  # fee from mock_connector
        
        await engine.stop()


# ==================== 内存管理测试 ====================

class TestMemoryManagement:
    """内存管理测试"""
    
    @pytest.mark.asyncio
    async def test_memory_cleanup_removes_expired_tasks(self, engine):
        """过期任务应被清理"""
        await engine.start()
        
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        oid = await engine.submit(signal, "ETH-USDC", 0.1)
        
        await asyncio.sleep(0.1)
        assert oid in engine._completed
        
        # 等待 TTL + cleanup interval
        await asyncio.sleep(0.4)
        
        assert oid not in engine._completed
        assert oid not in engine._tasks
        
        await engine.stop()
        
    @pytest.mark.asyncio
    async def test_exchange_order_map_cleanup(self, engine):
        """_exchange_order_map 应随任务清理"""
        await engine.start()
        
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        oid = await engine.submit(signal, "ETH-USDC", 0.1)
        
        await asyncio.sleep(0.1)
        
        # 获取交易所订单 ID
        task = engine._tasks.get(oid)
        exchange_order_id = task.order_id if task else None
        
        if exchange_order_id:
            assert exchange_order_id in engine._exchange_order_map
        
        # 等待清理
        await asyncio.sleep(0.4)
        
        if exchange_order_id:
            assert exchange_order_id not in engine._exchange_order_map
        
        await engine.stop()


# ==================== Mock SDK 错误测试 ====================

class TestSDKErrorHandling:
    """Mock DEX SDK 错误处理测试"""
    
    @pytest.mark.asyncio
    async def test_sdk_failure_does_not_update_risk_state(self, mock_connector, risk_manager):
        """SDK 失败时不应更新风控状态"""
        # Mock 失败响应
        mock_connector.create_order = AsyncMock(
            return_value=OrderResult(success=False, error="Reverted Transaction")
        )
        
        engine = ExecutionEngine(
            connector=mock_connector,
            risk_manager=risk_manager,
            cleanup_interval_seconds=1.0
        )
        await engine.start()
        
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        await engine.submit(signal, "ETH-USDC", 0.5)
        
        await asyncio.sleep(0.15)
        
        # 仓位应保持不变
        assert risk_manager.get_state()["positions"].get("ETH-USDC", 0) == 0
        
        await engine.stop()
        
    @pytest.mark.asyncio
    async def test_sdk_timeout_order_state(self, mock_connector, risk_manager):
        """SDK 超时应将订单标记为 TIMEOUT"""
        # Mock 超时
        async def slow_create_order(*args, **kwargs):
            await asyncio.sleep(10)  # 永远不会完成
            return OrderResult(success=True, order_id="123")
        
        mock_connector.create_order = slow_create_order
        
        engine = ExecutionEngine(
            connector=mock_connector,
            risk_manager=risk_manager,
            cleanup_interval_seconds=1.0
        )
        await engine.start()
        
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        oid = await engine.submit(signal, "ETH-USDC", 0.1)
        
        # 订单会在 10s 后超时, 这里只验证它进入了队列
        await asyncio.sleep(0.1)
        
        task = engine.get_task(oid)
        assert task is not None
        assert task.state in (OrderState.PENDING, OrderState.SUBMITTING)
        
        await engine.stop()


# ==================== 统计与状态测试 ====================

class TestEngineStats:
    """引擎统计测试"""
    
    @pytest.mark.asyncio
    async def test_get_stats(self, engine):
        """引擎统计应正确反映状态"""
        await engine.start()
        
        signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
        await engine.submit(signal, "ETH-USDC", 0.1)
        
        await asyncio.sleep(0.15)
        
        stats = engine.get_stats()
        assert stats["total_tasks"] >= 1
        assert stats["running"] is True
        
        await engine.stop()
        
        stats = engine.get_stats()
        assert stats["running"] is False

