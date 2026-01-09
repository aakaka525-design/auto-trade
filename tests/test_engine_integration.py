import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock
from engine.execution_engine import ExecutionEngine, OrderTask
from risk.manager import RiskManager, RiskConfig, RiskLimitExceededError
from connectors.base import BaseConnector, OrderResult
from strategies.base import Signal, SignalAction

@pytest.fixture
def mock_connector():
    connector = MagicMock(spec=BaseConnector)
    connector.create_order = AsyncMock(return_value=OrderResult(success=True, order_id="123", average_price=2000.0))
    return connector

@pytest.fixture
def risk_manager():
    config = RiskConfig(
        max_position_size={"ETH-USDC": 1.0},
        max_single_order_size={"ETH-USDC": 1.0}
    )
    return RiskManager(config)

@pytest.fixture
def engine(mock_connector, risk_manager):
    return ExecutionEngine(
        connector=mock_connector, 
        risk_manager=risk_manager,
        cleanup_interval_seconds=0.1
    )

@pytest.mark.asyncio
async def test_risk_check_interception(engine):
    """Test that engine rejects orders when RiskManager raises exception"""
    await engine.start()
    
    # 1. Valid Order
    signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
    await engine.submit(signal, "ETH-USDC", 0.5)
    
    # Wait for processing
    await asyncio.sleep(0.1)
    assert engine.risk_manager.get_state()["positions"]["ETH-USDC"] == 0.5
    
    # 2. Invalid Order (Exceeds Position Limit 0.5 + 0.6 > 1.0)
    # The submit method calls check_order implicitly
    signal_large = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
    
    with pytest.raises(RiskLimitExceededError):
        await engine.submit(signal_large, "ETH-USDC", 0.6)
        
    await engine.stop()

@pytest.mark.asyncio
async def test_memory_cleanup(engine):
    """Test background task cleanup"""
    # Set very short TTL for test
    engine._task_ttl = 0.1 
    await engine.start()
    
    # Submit order
    signal = Signal(action=SignalAction.BUY, price=2000.0, confidence=1.0)
    oid = await engine.submit(signal, "ETH-USDC", 0.1)
    
    await asyncio.sleep(0.1) # Wait for execution
    assert oid in engine._completed
    
    # Wait for cleanup (cleanup interval 0.1s)
    await asyncio.sleep(0.3)
    
    # Should be gone
    assert oid not in engine._completed
    assert oid not in engine._tasks
    
    await engine.stop()
