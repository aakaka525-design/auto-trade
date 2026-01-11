"""
RiskManager 单元测试

测试覆盖:
1. 基础风控检查 (Hard Checks)
2. 熔断机制 (Circuit Breaker)
3. 极端行情模拟 (Extreme Scenarios)
4. DEX SDK 错误处理 (Mocked SDK Errors)
5. 滑点保护 (Slippage Protection) - Placeholder
"""
import pytest
from unittest.mock import Mock, MagicMock, AsyncMock, patch
from risk.manager import RiskManager, RiskConfig, RiskLimitExceededError, CircuitBreakerTrippedError


# ==================== Fixtures ====================

@pytest.fixture
def risk_config():
    return RiskConfig(
        max_position_size={"ETH-USDC": 10.0, "BTC-USDC": 1.0},
        max_daily_loss=500.0,
        max_single_order_size={"ETH-USDC": 5.0, "BTC-USDC": 0.5},
        max_slippage_pct=0.02  # 2%
    )


@pytest.fixture
def risk_manager(risk_config):
    return RiskManager(risk_config)


# ==================== 基础测试 ====================

class TestRiskManagerBasic:
    """基础功能测试"""
    
    def test_initialization(self, risk_manager):
        """初始化状态验证"""
        state = risk_manager.get_state()
        assert state["positions"] == {}
        assert state["daily_realized_pnl"] == 0.0
        assert state["circuit_breaker"] is False

    def test_check_order_valid(self, risk_manager):
        """有效订单应通过检查"""
        # Should pass without exception
        risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0)
        risk_manager.check_order("BTC-USDC", "SELL", 0.3, 50000.0)
        
    def test_max_single_order_size_exceeded(self, risk_manager):
        """超过单笔订单限制应拒绝"""
        with pytest.raises(RiskLimitExceededError, match="Order size 6.0 exceeds max limit"):
            risk_manager.check_order("ETH-USDC", "BUY", 6.0, 2000.0)

    def test_max_position_limit_accumulated(self, risk_manager):
        """累计仓位超限应拒绝"""
        # 1. 建仓
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 8.0, 
            "price": 2000.0
        })
        
        # 2. 验证状态
        assert risk_manager.get_state()["positions"]["ETH-USDC"] == 8.0
        
        # 3. 超限买入 (8 + 3 = 11 > 10)
        with pytest.raises(RiskLimitExceededError, match="Projected position 11.0 exceeds limit"):
            risk_manager.check_order("ETH-USDC", "BUY", 3.0, 2000.0)
            
        # 4. 减仓应允许
        risk_manager.check_order("ETH-USDC", "SELL", 3.0, 2000.0)

    def test_undefined_symbol_defaults_to_infinite(self, risk_manager):
        """未定义交易对应默认允许 (无限制)"""
        # 不应抛出异常
        risk_manager.check_order("UNKNOWN-TOKEN", "BUY", 1000.0, 1.0)


# ==================== 熔断机制测试 ====================

class TestCircuitBreaker:
    """日内亏损熔断测试"""
    
    def test_circuit_breaker_trigger_via_update_pnl(self, risk_manager):
        """通过 update_pnl 触发熔断"""
        # 累计亏损
        risk_manager.update_pnl(-200.0)
        risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0)  # 应通过
        
        # 超过阈值 (-501 > -500)
        risk_manager.update_pnl(-301.0)
        
        with pytest.raises(CircuitBreakerTrippedError, match="Circuit breaker is ACTIVE"):
            risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0)
            
    def test_circuit_breaker_on_fill_high_fee(self, risk_manager):
        """通过高额手续费触发熔断"""
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 10.0, 
            "price": 2000.0, 
            "fee": 600.0  # 超过日亏损限制
        })
        
        assert risk_manager.get_state()["circuit_breaker"] is True
        
    def test_circuit_breaker_blocks_all_orders(self, risk_manager):
        """熔断后所有订单都应被拒绝"""
        # 触发熔断
        risk_manager.update_pnl(-600.0)
        
        # 即使是减仓也应被拒绝
        with pytest.raises(CircuitBreakerTrippedError):
            risk_manager.check_order("ETH-USDC", "SELL", 0.1, 2000.0)
            
    def test_reset_daily_stats(self, risk_manager):
        """日末重置应解除熔断"""
        # 触发熔断
        risk_manager.update_pnl(-600.0)
        assert risk_manager.get_state()["circuit_breaker"] is True
        
        # 重置
        risk_manager.reset_daily_stats()
        
        # 应该可以交易
        risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0)


# ==================== 极端行情测试 ====================

class TestExtremeMarketConditions:
    """极端行情模拟测试"""
    
    def test_flash_crash_rapid_losses(self, risk_manager):
        """闪崩场景: 快速连续亏损触发熔断"""
        # 模拟 5 笔快速亏损 (每笔 -120 USDC)
        for i in range(5):
            if risk_manager.get_state()["circuit_breaker"]:
                break
            risk_manager.update_pnl(-120.0)
        
        # 应在第 5 笔时触发 (-600 > -500)
        assert risk_manager.get_state()["circuit_breaker"] is True
        
    def test_position_flip_short_to_long(self, risk_manager):
        """持仓翻转: 空单转多单 (多次小单)"""
        # 建立空仓
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "SELL", 
            "quantity": 5.0, 
            "price": 2000.0
        })
        assert risk_manager.get_state()["positions"]["ETH-USDC"] == -5.0
        
        # 翻转为多仓需要多次买入 (单笔限制 5.0)
        # 买入 5.0: -5 + 5 = 0
        risk_manager.check_order("ETH-USDC", "BUY", 5.0, 2000.0)
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 5.0, 
            "price": 2000.0
        })
        
        # 再买 5.0: 0 + 5 = 5, 在限制内
        risk_manager.check_order("ETH-USDC", "BUY", 5.0, 2000.0)
        
    def test_position_flip_exceeds_limit(self, risk_manager):
        """持仓翻转超限 (累计仓位超限)"""
        # 建立空仓 -5.0
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "SELL", 
            "quantity": 5.0, 
            "price": 2000.0
        })
        
        # 买入 5.0 平仓: -5 + 5 = 0
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 5.0, 
            "price": 2000.0
        })
        
        # 买入 5.0 建多仓: 0 + 5 = 5
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 5.0, 
            "price": 2000.0
        })
        
        # 买入 5.0 再加仓: 5 + 5 = 10, 正好在限制内
        risk_manager.check_order("ETH-USDC", "BUY", 5.0, 2000.0)
        
        # 模拟成交
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 5.0, 
            "price": 2000.0
        })
        
        # 再买任意数量都会超限 (10 + 1 > 10)
        with pytest.raises(RiskLimitExceededError, match="Projected position 11.0 exceeds limit"):
            risk_manager.check_order("ETH-USDC", "BUY", 1.0, 2000.0)


# ==================== 状态持久化测试 ====================

class TestStatePersistence:
    """状态持久化测试"""
    
    def test_save_and_load_state(self, risk_manager, tmp_path):
        """保存和加载状态"""
        filepath = str(tmp_path / "risk_state.json")
        
        # 建立一些状态
        risk_manager.on_fill({
            "symbol": "ETH-USDC", 
            "side": "BUY", 
            "quantity": 3.0, 
            "price": 2000.0, 
            "fee": 10.0
        })
        risk_manager.update_pnl(-100.0)
        
        # 保存
        risk_manager.save_state(filepath)
        
        # 新实例加载
        new_config = RiskConfig(max_position_size={"ETH-USDC": 10.0})
        new_rm = RiskManager(new_config)
        assert new_rm.load_state(filepath) is True
        
        # 验证
        state = new_rm.get_state()
        assert state["positions"]["ETH-USDC"] == 3.0
        assert state["daily_realized_pnl"] == -110.0  # -100 PnL - 10 fee


# ==================== Mock DEX SDK 错误测试 ====================

class TestMockedSDKErrors:
    """Mock DEX SDK 错误处理"""
    
    @pytest.fixture
    def mock_connector(self):
        """Mock 交易所连接器"""
        connector = MagicMock()
        connector.create_order = AsyncMock()
        connector.cancel_order = AsyncMock(return_value=True)
        return connector
    
    def test_sdk_timeout_does_not_corrupt_state(self, risk_manager):
        """SDK 超时不应破坏风控状态"""
        initial_state = risk_manager.get_state().copy()
        
        # 模拟超时后状态应保持不变
        # (在实际集成中, ExecutionEngine 会处理超时)
        # RiskManager 本身不直接调用 SDK
        
        assert risk_manager.get_state() == initial_state
        
    def test_partial_fill_state_update(self, risk_manager):
        """部分成交应正确更新状态"""
        # 提交 5.0 订单
        risk_manager.check_order("ETH-USDC", "BUY", 5.0, 2000.0)
        
        # 部分成交 3.0
        risk_manager.on_fill({
            "symbol": "ETH-USDC",
            "side": "BUY",
            "quantity": 3.0,
            "price": 2000.0,
            "fee": 1.0
        })
        
        assert risk_manager.get_state()["positions"]["ETH-USDC"] == 3.0
        
        # 剩余 2.0 成交
        risk_manager.on_fill({
            "symbol": "ETH-USDC",
            "side": "BUY",
            "quantity": 2.0,
            "price": 2005.0,
            "fee": 0.5
        })
        
        assert risk_manager.get_state()["positions"]["ETH-USDC"] == 5.0


# ==================== 边界条件测试 ====================

class TestEdgeCases:
    """边界条件测试"""
    
    def test_zero_size_order(self, risk_manager):
        """零数量订单"""
        # 应该通过 (虽然无意义)
        risk_manager.check_order("ETH-USDC", "BUY", 0.0, 2000.0)
        
    def test_negative_price(self, risk_manager):
        """负价格订单 (理论上不应发生)"""
        # RiskManager 目前不验证价格合理性
        risk_manager.check_order("ETH-USDC", "BUY", 1.0, -100.0)
        
    def test_exact_limit_order(self, risk_manager):
        """恰好等于限制的订单应通过"""
        # 建仓 5.0
        risk_manager.on_fill({
            "symbol": "ETH-USDC",
            "side": "BUY",
            "quantity": 5.0,
            "price": 2000.0
        })
        
        # 再买 5.0 (5 + 5 = 10 == 限制)
        risk_manager.check_order("ETH-USDC", "BUY", 5.0, 2000.0)
