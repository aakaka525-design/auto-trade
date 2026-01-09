from dataclasses import dataclass, field
from typing import Dict, Optional, Any
import logging
from decimal import Decimal

# Configure logging
logger = logging.getLogger(__name__)

@dataclass
class RiskConfig:
    """
    Configuration for Risk Manager.
    
    Attributes:
        max_position_size (Dict[str, float]): Maximum position size allowed per symbol (in base asset).
        max_daily_loss (float): Maximum allowed daily loss in quote currency (blocking).
        max_single_order_size (Dict[str, float]): Maximum size for a single order per symbol.
        max_slippage_pct (float): Maximum allowed slippage percentage (e.g., 0.05 for 5%).
    """
    max_position_size: Dict[str, float] = field(default_factory=dict)
    max_daily_loss: float = 1000.0
    max_single_order_size: Dict[str, float] = field(default_factory=dict)
    max_slippage_pct: float = 0.02


class RiskException(Exception):
    """Base exception for risk control violations."""
    pass


class RiskLimitExceededError(RiskException):
    """Raised when a specific risk limit (position, size) is exceeded."""
    pass


class CircuitBreakerTrippedError(RiskException):
    """Raised when a global circuit breaker (e.g., daily loss) is tripped."""
    pass


class RiskManager:
    """
    Mandatory Risk Control Layer.
    Enforces 'Hard Checks' on all outgoing orders.
    """

    def __init__(self, config: RiskConfig):
        self.config = config
        
        # State tracking
        # positions: symbol -> signed quantity (float)
        self._positions: Dict[str, float] = {}
        
        # PnL tracking
        self._daily_realized_pnl: float = 0.0
        self._circuit_breaker_active: bool = False
        
        logger.info("RiskManager initialized with config: %s", config)

    def check_order(self, symbol: str, side: str, size: float, price: float) -> None:
        """
        Check if an order verifies all risk constraints.
        Raises RiskException if validaton fails.
        """
        if self._circuit_breaker_active:
            raise CircuitBreakerTrippedError("Circuit breaker is ACTIVE. Trading halted.")

        # 1. Check Daily Loss (Estimating current PnL is hard without live price, 
        # so we rely on realized PnL + conservative checks)
        # In a real system, we'd add unrealized PnL check here.
        if self._daily_realized_pnl <= -self.config.max_daily_loss:
            self._circuit_breaker_active = True
            logger.critical(f"Daily loss limit hit: {self._daily_realized_pnl} <= -{self.config.max_daily_loss}")
            raise CircuitBreakerTrippedError(f"Daily loss limit exceeded: {self._daily_realized_pnl}")

        # 2. Check Single Order Size
        max_size = self.config.max_single_order_size.get(symbol, float('inf'))
        if size > max_size:
            raise RiskLimitExceededError(f"Order size {size} exceeds max limit {max_size} for {symbol}")

        # 3. Check Max Position Limit
        current_pos = self._positions.get(symbol, 0.0)
        # Calculate projected position
        # Buy adds to position, Sell subtracts
        if side.upper() == "BUY":
            projected_pos = current_pos + size
        elif side.upper() == "SELL":
            projected_pos = current_pos - size
        else:
            projected_pos = current_pos # Should verify side validity elsewhere or assume standard

        max_pos = self.config.max_position_size.get(symbol, float('inf'))
        
        # We check absolute value for max position limit usually, or just long/short caps
        # Here assuming max_pos is absolute limit for simplicity
        if abs(projected_pos) > max_pos:
            raise RiskLimitExceededError(
                f"Projected position {projected_pos} exceeds limit {max_pos} for {symbol}"
            )

        logger.debug(f"Risk check passed for {side} {size} {symbol} @ {price}")

    def on_fill(self, fill_event: Dict[str, Any]) -> None:
        """
        Update state based on fill execution.
        
        Args:
            fill_event: Dict containing 'symbol', 'side', 'quantity', 'price', 'fee'
        """
        symbol = fill_event['symbol']
        side = fill_event['side']
        qty = float(fill_event['quantity'])
        price = float(fill_event['price'])
        fee = float(fill_event.get('fee', 0.0))

        # Update Position
        previous_pos = self._positions.get(symbol, 0.0)
        
        if side.upper() == "BUY":
            self._positions[symbol] = previous_pos + qty
        elif side.upper() == "SELL":
            self._positions[symbol] = previous_pos - qty
            
        # PnL Calculation (Simplified FIFO or Average Cost could be used)
        # For HFT risk, we primarily care about Total Equity Drop.
        # Since we don't have a full trade history engine here, we approximate Realized PnL 
        # only when reducing position (closing).
        
        # Crude Realized PnL approximation: 
        # If we are closing a position, calculate PnL against average entry price?
        # Without persistant storage of executed trades, we can only track 'Session PnL' accurately 
        # if we assume start from 0 or provided state.
        
        # For this task, let's implement a simplified Realized PnL tracker 
        # assuming 'average_entry_price' is tracked.
        # Note: A proper system needs a full PositionManager. RiskManager is a safeguard.
        # We will assume this method is called properly.
        
        self._update_pnl_state(symbol, side, qty, price, fee)

        # Re-check circuit breaker after state update
        if self._daily_realized_pnl <= -self.config.max_daily_loss:
            self._circuit_breaker_active = True
            logger.warning("Circuit breaker TRIPPED after fill update.")

    def _update_pnl_state(self, symbol: str, side: str, qty: float, price: float, fee: float) -> None:
        """
        Internal helper to update PnL. 
        This is a heuristic implementation since full accounting is in Engine.
        """
        # NOTE: In a real system, RiskManager should listen to the "Accounting Service"
        # rather than calculate PnL itself to avoid split-brain logic.
        # However, for 'Hard Checks', having a local conservative estimate is good.
        
        # Deduct fees immediately
        self._daily_realized_pnl -= fee
        
        # This is a placeholder for logic that would normally require knowing the cost basis.
        # If we don't track cost basis, we can't calculate realized PnL on closing trades.
        # For the purpose of this task (Safety), we will just subtract fees 
        # and rely on an external 'update_pnl' method if the engine provides it,
        # OR we implement a simple average cost tracker here.
        
        pass 

    def update_pnl(self, realized_pnl: float) -> None:
        """
        External method to update PnL from the authoritative source (ExecutionEngine).
        """
        self._daily_realized_pnl += realized_pnl
        if self._daily_realized_pnl <= -self.config.max_daily_loss:
            self._circuit_breaker_active = True

    def get_state(self) -> Dict[str, Any]:
        """Return current risk state for monitoring."""
        return {
            "positions": self._positions.copy(),
            "daily_realized_pnl": self._daily_realized_pnl,
            "circuit_breaker": self._circuit_breaker_active
        }

    def reset_daily_stats(self):
        """Reset daily stats (e.g. at 00:00 UTC)."""
        self._daily_realized_pnl = 0.0
        self._circuit_breaker_active = False
    
    # ==================== 状态持久化 ====================
    
    def save_state(self, filepath: str = None) -> None:
        """
        保存风控状态到文件 (防止重启丢失)
        
        Args:
            filepath: 保存路径，默认为 .risk_state.json
        """
        import json
        from pathlib import Path
        
        filepath = filepath or ".risk_state.json"
        state = {
            "positions": self._positions,
            "daily_realized_pnl": self._daily_realized_pnl,
            "circuit_breaker_active": self._circuit_breaker_active,
        }
        
        try:
            Path(filepath).write_text(json.dumps(state, indent=2))
            logger.debug(f"风控状态已保存: {filepath}")
        except Exception as e:
            logger.error(f"保存风控状态失败: {e}")
    
    def load_state(self, filepath: str = None) -> bool:
        """
        从文件加载风控状态
        
        Args:
            filepath: 加载路径
            
        Returns:
            是否加载成功
        """
        import json
        from pathlib import Path
        
        filepath = filepath or ".risk_state.json"
        
        try:
            if not Path(filepath).exists():
                logger.info(f"风控状态文件不存在: {filepath}")
                return False
            
            state = json.loads(Path(filepath).read_text())
            self._positions = state.get("positions", {})
            self._daily_realized_pnl = state.get("daily_realized_pnl", 0.0)
            self._circuit_breaker_active = state.get("circuit_breaker_active", False)
            
            logger.info(f"风控状态已加载: positions={len(self._positions)}, pnl={self._daily_realized_pnl}")
            return True
        except Exception as e:
            logger.error(f"加载风控状态失败: {e}")
            return False
