"""
交易所连接器抽象基类

设计模式: Adapter Pattern
所有交易所实现必须继承此基类，确保接口一致性。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Optional, Any
from enum import Enum


# ==================== Global ID 定义 ====================

class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    FAILED = "failed"


# ==================== 数据结构 ====================

@dataclass
class Candlestick:
    """K 线数据"""
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderBookLevel:
    """订单簿档位"""
    price: float
    size: float


@dataclass
class OrderBook:
    """订单簿快照"""
    symbol: str
    timestamp: datetime
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    
    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None
    
    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None
    
    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return None
    
    @property
    def spread(self) -> Optional[float]:
        if self.best_ask is not None and self.best_bid is not None:
            return self.best_ask - self.best_bid
        return None


@dataclass
class Trade:
    """成交记录"""
    trade_id: str
    symbol: str
    price: float
    size: float
    side: OrderSide
    timestamp: datetime


@dataclass
class Order:
    """订单"""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: float
    size: float
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_global_id(self) -> str:
        """生成 Global Order ID (唯一)"""
        ts_ms = int(self.created_at.timestamp() * 1000)  # 毫秒级
        seq = id(self) % 10000  # 对象 ID 取模作为序列号
        return f"ORD_{self.side.value.upper()}_{ts_ms}_{seq:04d}"


@dataclass
class Position:
    """持仓"""
    symbol: str
    side: Optional[OrderSide]
    size: float
    entry_price: float
    unrealized_pnl: float = 0.0
    liquidation_price: Optional[float] = None


@dataclass
class AccountInfo:
    """账户信息"""
    account_id: str
    available_balance: float
    total_equity: float
    positions: list[Position] = field(default_factory=list)


@dataclass
class OrderResult:
    """订单执行结果"""
    success: bool
    order_id: Optional[str] = None
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    average_price: Optional[float] = None
    fee: Optional[float] = None
    
    @classmethod
    def ok(cls, order_id: str, tx_hash: str = None) -> "OrderResult":
        return cls(success=True, order_id=order_id, tx_hash=tx_hash)
    
    @classmethod
    def fail(cls, error: str) -> "OrderResult":
        return cls(success=False, error=error)


# ==================== 抽象基类 ====================

class BaseConnector(ABC):
    """
    交易所连接器抽象基类
    
    所有交易所实现必须继承此类并实现所有抽象方法。
    使用 Adapter Pattern 隔离不同交易所 SDK 的差异。
    """
    
    def __init__(self, config: dict):
        self.config = config
        self._connected = False
    
    @property
    def is_connected(self) -> bool:
        return self._connected
    
    # ==================== 连接管理 ====================
    
    @abstractmethod
    async def connect(self) -> bool:
        """
        建立与交易所的连接
        
        Returns:
            连接是否成功
        """
        ...
    
    @abstractmethod
    async def disconnect(self) -> None:
        """断开连接"""
        ...
    
    # ==================== 行情数据 ====================
    
    @abstractmethod
    async def get_orderbook(self, symbol: str) -> OrderBook:
        """
        获取订单簿快照
        
        Args:
            symbol: 交易对 (如 "ETH-USDC")
            
        Returns:
            OrderBook 对象
        """
        ...
    
    @abstractmethod
    async def get_candlesticks(
        self, 
        symbol: str, 
        interval: str, 
        limit: int = 100
    ) -> list[Candlestick]:
        """
        获取 K 线数据
        
        Args:
            symbol: 交易对
            interval: 时间周期 ("1m", "5m", "15m", "1h", "4h", "1D")
            limit: 获取数量
            
        Returns:
            Candlestick 列表
        """
        ...
    
    @abstractmethod
    async def get_ticker_price(self, symbol: str) -> float:
        """获取最新价格"""
        ...
    
    # ==================== 账户信息 ====================
    
    @abstractmethod
    async def get_account(self) -> AccountInfo:
        """获取账户信息"""
        ...
    
    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        """获取指定交易对的持仓"""
        ...
    
    # ==================== 订单管理 ====================
    
    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        size: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False
    ) -> OrderResult:
        """
        创建订单
        
        Args:
            symbol: 交易对
            side: 买/卖方向
            order_type: 订单类型
            size: 订单数量 (base 资产)
            price: 限价 (市价单可为 None)
            stop_price: 触发价格 (止损/止盈单)
            reduce_only: 是否仅减仓
            
        Returns:
            OrderResult 对象
        """
        ...
    
    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        ...
    
    @abstractmethod
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """取消所有订单"""
        ...
    
    # ==================== 实时数据流 ====================
    
    @abstractmethod
    async def stream_orderbook(self, symbol: str) -> AsyncIterator[OrderBook]:
        """
        订阅订单簿实时更新
        
        Yields:
            OrderBook 更新
        """
        ...
    
    @abstractmethod
    async def stream_trades(self, symbol: str) -> AsyncIterator[Trade]:
        """
        订阅成交流
        
        Yields:
            Trade 成交记录
        """
        ...
