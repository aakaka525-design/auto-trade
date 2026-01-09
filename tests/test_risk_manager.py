import pytest
from unittest.mock import Mock, MagicMock
from risk.manager import RiskManager, RiskConfig, RiskLimitExceededError, CircuitBreakerTrippedError

@pytest.fixture
def risk_config():
    return RiskConfig(
        max_position_size={"ETH-USDC": 10.0, "BTC-USDC": 1.0},
        max_daily_loss=500.0,
        max_single_order_size={"ETH-USDC": 5.0, "BTC-USDC": 0.5},
        max_slippage_pct=0.01
    )

@pytest.fixture
def risk_manager(risk_config):
    return RiskManager(risk_config)

class TestRiskManager:
    
    def test_initialization(self, risk_manager):
        state = risk_manager.get_state()
        assert state["positions"] == {}
        assert state["daily_realized_pnl"] == 0.0
        assert state["circuit_breaker"] is False

    def test_check_order_valid(self, risk_manager):
        # Should pass
        risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0)
        
    def test_max_single_order_size_exceeded(self, risk_manager):
        with pytest.raises(RiskLimitExceededError, match="Order size 6.0 exceeds max limit"):
            risk_manager.check_order("ETH-USDC", "BUY", 6.0, 2000.0)

    def test_max_position_limit_accumulated(self, risk_manager):
        # 1. Fill some orders to build position
        risk_manager.on_fill({"symbol": "ETH-USDC", "side": "BUY", "quantity": 8.0, "price": 2000.0})
        
        # 2. Check internal state
        assert risk_manager.get_state()["positions"]["ETH-USDC"] == 8.0
        
        # 3. Try to buy 3.0 more (Total 11.0 > Limit 10.0)
        with pytest.raises(RiskLimitExceededError, match="Projected position 11.0 exceeds limit"):
            risk_manager.check_order("ETH-USDC", "BUY", 3.0, 2000.0)
            
        # 4. Sell should be allowed (reducing position)
        risk_manager.check_order("ETH-USDC", "SELL", 3.0, 2000.0)

    def test_circuit_breaker_trigger(self, risk_manager):
        # Simulate a series of losses
        # Loss 1: -200
        risk_manager.update_pnl(-200.0)
        risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0) # Should pass
        
        # Loss 2: -301 (Total -501 > Limit 500)
        risk_manager.update_pnl(-301.0)
        
        # Check calling check_order now raises CircuitBreaker
        with pytest.raises(CircuitBreakerTrippedError, match="Circuit breaker is ACTIVE"):
            risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0)
            
    def test_circuit_breaker_on_fill_update(self, risk_manager):
        # If PnL updates via on_fill trigger the breaker
        # Currently on_fill only subtracts fees in our simplified implementation
        # Let's say fees are huge
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 10.0, 
            "price": 2000.0, 
            "fee": 600.0
        })
        
        assert risk_manager.get_state()["circuit_breaker"] is True
        
        with pytest.raises(CircuitBreakerTrippedError, match="Circuit breaker is ACTIVE"):
            risk_manager.check_order("ETH-USDC", "BUY", 0.1, 2000.0)

    def test_undefined_symbol_defaults(self, risk_manager):
        # Symbol not in config should default to inf limit for size, but let's check behavior
        # In current implementation, get(symbol, inf) is used.
        risk_manager.check_order("UNKNOWN-TOKEN", "BUY", 1000.0, 1.0)
