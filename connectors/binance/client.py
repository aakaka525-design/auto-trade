"""
Binance Spot 交易所连接器

实现 BaseConnector 接口，封装 Binance REST API。
支持 Spot 交易、行情数据、账户信息查询。

SDK 参考: https://github.com/binance/binance-spot-api-docs
"""
import asyncio
import logging
from datetime import datetime
from typing import AsyncIterator, Optional, Dict, Any, List

import aiohttp

from connectors.base import (
    BaseConnector,
    OrderBook,
    OrderBookLevel,
    Candlestick,
    Trade,
    Order,
    Position,
    AccountInfo,
    OrderResult,
    OrderSide,
    OrderType,
    OrderStatus,
)
from connectors.binance.auth import BinanceAuth, SymbolConverter
from connectors.retry import TokenBucketLimiter, RetryConfig, retry_async
from core.exceptions import (
    TradingError,
    RateLimitExceededError,
    OrderRejectedError,
    InsufficientBalanceError,
)

logger = logging.getLogger(__name__)


# ==================== Binance 错误码映射 ====================

class BinanceAPIError(TradingError):
    """Binance API 错误"""
    
    # 可重试的错误码
    RETRYABLE_CODES = {-1000, -1015, -1021}
    
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        self.retryable = code in self.RETRYABLE_CODES
        super().__init__(f"[{code}] {message}")


# ==================== Binance 连接器 ====================

class BinanceConnector(BaseConnector):
    """
    Binance Spot 交易所连接器
    
    功能:
    - REST API 行情/交易
    - HMAC-SHA256 签名认证
    - 自动限流 (1200 权重/分钟)
    - 服务器时间同步
    - 异常处理和重试
    
    配置示例:
    ```python
    config = {
        "api_key": "your_api_key",
        "api_secret": "your_api_secret",
        "testnet": True,  # 使用测试网
    }
    connector = BinanceConnector(config)
    await connector.connect()
    ```
    """
    
    # API 端点
    MAINNET_URL = "https://api.binance.com"
    TESTNET_URL = "https://testnet.binance.vision"
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        self.exchange_name = "binance"
        
        # 配置解析
        self._api_key = config.get("api_key", "")
        self._api_secret = config.get("api_secret", "")
        self._private_key = config.get("private_key", "")
        self._sign_type = config.get("sign_type", "HMAC")
        self._testnet = config.get("testnet", False)
        
        # 基础 URL
        self._base_url = self.TESTNET_URL if self._testnet else self.MAINNET_URL
        
        # 认证管理器
        self._auth: Optional[BinanceAuth] = None
        
        # HTTP 会话
        self._session: Optional[aiohttp.ClientSession] = None
        
        # 限流器 (Binance: 1200 权重/分钟)
        self._rate_limiter = TokenBucketLimiter(
            rate=20,  # 每秒 20 权重
            capacity=1200
        )
        
        # 交易所信息缓存
        self._exchange_info: Dict[str, Any] = {}
        self._symbol_info: Dict[str, Any] = {}
    
    # ==================== 连接管理 ====================
    
    async def connect(self) -> bool:
        """建立连接并初始化"""
        try:
            logger.info(f"连接 Binance {'Testnet' if self._testnet else 'Mainnet'}...")
            
            # 创建 HTTP 会话
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            
            # 初始化认证
            if self._api_key:
                if self._sign_type == "Ed25519" and self._private_key:
                    self._auth = BinanceAuth(
                        api_key=self._api_key,
                        private_key=self._private_key,
                        sign_type="Ed25519"
                    )
                    logger.info("使用 Ed25519 签名认证")
                elif self._api_secret:
                    self._auth = BinanceAuth(
                        api_key=self._api_key,
                        api_secret=self._api_secret,
                        sign_type="HMAC"
                    )
                    logger.info("使用 HMAC 签名认证")
                
                if self._auth:
                    await self._auth.sync_server_time(self._base_url)
            
            # 加载交易所信息
            await self._load_exchange_info()
            
            self._connected = True
            logger.info(f"✅ Binance 连接成功 ({len(self._symbol_info)} 个交易对)")
            return True
            
        except Exception as e:
            logger.error(f"Binance 连接失败: {e}")
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        if self._session:
            await self._session.close()
            self._session = None
        self._connected = False
        logger.info("Binance 连接已断开")
    
    async def _ensure_connected(self) -> None:
        """确保连接可用，必要时自动重连"""
        if self._session is None or self._session.closed:
            logger.warning("会话已关闭，尝试自动重连...")
            success = await self.connect()
            if not success:
                raise TradingError("自动重连 Binance 失败")
    
    async def _load_exchange_info(self) -> None:
        """加载交易所信息 (交易对规则)"""
        # 跳过 ensure_connected 避免循环（connect 内会调用此方法）
        if not self._session:
            return
        data = await self._request("GET", "/api/v3/exchangeInfo")
        self._exchange_info = data
        
        # 建立交易对索引
        for symbol_data in data.get("symbols", []):
            binance_symbol = symbol_data["symbol"]
            self._symbol_info[binance_symbol] = symbol_data
    
    # ==================== HTTP 请求 ====================
    
    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        weight: int = 1,
    ) -> Dict[str, Any]:
        """
        发送 HTTP 请求
        
        Args:
            method: HTTP 方法
            endpoint: API 端点
            params: 请求参数
            signed: 是否需要签名
            weight: 请求权重 (限流)
            
        Returns:
            响应 JSON
        """
        # 自动重连检查
        await self._ensure_connected()
        
        # 限流
        await self._rate_limiter.acquire(weight)
        
        # 准备参数
        params = params or {}
        headers = {}
        
        if signed:
            if not self._auth:
                raise TradingError("需要 API 认证")
            params = self._auth.sign_params(params)
            headers = self._auth.get_headers()
        elif self._auth:
            headers = {'X-MBX-APIKEY': self._auth.api_key}
        
        url = f"{self._base_url}{endpoint}"
        
        try:
            if method == "GET":
                async with self._session.get(url, params=params, headers=headers) as resp:
                    return await self._handle_response(resp)
            elif method == "POST":
                async with self._session.post(url, data=params, headers=headers) as resp:
                    return await self._handle_response(resp)
            elif method == "DELETE":
                async with self._session.delete(url, params=params, headers=headers) as resp:
                    return await self._handle_response(resp)
            else:
                raise ValueError(f"不支持的 HTTP 方法: {method}")
                
        except aiohttp.ClientError as e:
            raise TradingError(f"网络错误: {e}")
    
    async def _handle_response(self, resp: aiohttp.ClientResponse) -> Dict[str, Any]:
        """处理 API 响应"""
        data = await resp.json()
        
        if resp.status != 200:
            code = data.get("code", -1)
            msg = data.get("msg", "Unknown error")
            
            # 特殊错误处理
            if code == -1015:
                raise RateLimitExceededError(msg, retry_after=60)
            elif code == -2010:
                raise InsufficientBalanceError(msg)
            elif code == -2011:
                raise OrderRejectedError(msg)
            elif code == -1021:
                # 时间戳问题，重新同步
                if self._auth:
                    await self._auth.sync_server_time(self._base_url)
                raise BinanceAPIError(code, msg)
            else:
                raise BinanceAPIError(code, msg)
        
        return data
    
    # ==================== 行情数据 ====================
    
    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook:
        """获取订单簿快照"""
        binance_symbol = SymbolConverter.to_binance(symbol)
        
        data = await self._request(
            "GET", 
            "/api/v3/depth",
            params={"symbol": binance_symbol, "limit": depth},
            weight=5 if depth <= 100 else 10
        )
        
        bids = [
            OrderBookLevel(price=float(p), size=float(s))
            for p, s in data.get("bids", [])
        ]
        asks = [
            OrderBookLevel(price=float(p), size=float(s))
            for p, s in data.get("asks", [])
        ]
        
        return OrderBook(
            symbol=symbol,
            timestamp=datetime.now(),
            bids=bids,
            asks=asks
        )
    
    async def get_candlesticks(
        self,
        symbol: str,
        interval: str,
        limit: int = 100
    ) -> List[Candlestick]:
        """获取 K 线数据"""
        binance_symbol = SymbolConverter.to_binance(symbol)
        
        # 时间周期映射
        interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m",
            "1h": "1h", "4h": "4h", "1D": "1d", "1d": "1d"
        }
        binance_interval = interval_map.get(interval, "1m")
        
        data = await self._request(
            "GET",
            "/api/v3/klines",
            params={
                "symbol": binance_symbol,
                "interval": binance_interval,
                "limit": limit
            },
            weight=1
        )
        
        candles = []
        for item in data:
            candles.append(Candlestick(
                timestamp=item[0],
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5])
            ))
        
        return candles
    
    async def get_ticker_price(self, symbol: str) -> float:
        """获取最新价格"""
        binance_symbol = SymbolConverter.to_binance(symbol)
        
        data = await self._request(
            "GET",
            "/api/v3/ticker/price",
            params={"symbol": binance_symbol},
            weight=1
        )
        
        return float(data.get("price", 0))
    
    # ==================== 账户信息 ====================
    
    async def get_account(self) -> AccountInfo:
        """获取账户余额信息"""
        data = await self._request(
            "GET",
            "/api/v3/account",
            signed=True,
            weight=10
        )
        
        # 计算总权益 (简化: 只统计 USDT)
        balances = data.get("balances", [])
        usdt_balance = 0.0
        
        for bal in balances:
            if bal["asset"] == "USDT":
                usdt_balance = float(bal["free"]) + float(bal["locked"])
                break
        
        return AccountInfo(
            account_id=str(data.get("accountType", "SPOT")),
            available_balance=usdt_balance,
            total_equity=usdt_balance,
            positions=[]  # Spot 无持仓概念
        )
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """获取持仓 (Spot 无持仓概念)"""
        return None
    
    # ==================== 订单管理 ====================
    
    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        size: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """创建订单"""
        binance_symbol = SymbolConverter.to_binance(symbol)
        
        # 订单方向
        binance_side = "BUY" if side == OrderSide.BUY else "SELL"
        
        # 订单类型映射
        type_map = {
            OrderType.LIMIT: "LIMIT",
            OrderType.MARKET: "MARKET",
            OrderType.STOP_LOSS: "STOP_LOSS_LIMIT",
            OrderType.TAKE_PROFIT: "TAKE_PROFIT_LIMIT",
        }
        binance_type = type_map.get(order_type, "LIMIT")
        
        # 构建参数
        params: Dict[str, Any] = {
            "symbol": binance_symbol,
            "side": binance_side,
            "type": binance_type,
            "quantity": self._format_quantity(binance_symbol, size),
        }
        
        # 限价单需要价格
        if order_type == OrderType.LIMIT:
            if price is None:
                return OrderResult.fail("限价单需要指定价格")
            params["price"] = self._format_price(binance_symbol, price)
            params["timeInForce"] = "GTC"
        
        # 止损/止盈单需要触发价格
        if order_type in (OrderType.STOP_LOSS, OrderType.TAKE_PROFIT):
            if stop_price is None:
                return OrderResult.fail("止损/止盈单需要指定触发价格")
            params["stopPrice"] = self._format_price(binance_symbol, stop_price)
            if price:
                params["price"] = self._format_price(binance_symbol, price)
            params["timeInForce"] = "GTC"
        
        # 自定义订单 ID
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        
        try:
            data = await self._request(
                "POST",
                "/api/v3/order",
                params=params,
                signed=True,
                weight=1
            )
            
            return OrderResult.ok(
                order_id=str(data["orderId"]),
                tx_hash=None  # CEX 无 tx_hash
            )
            
        except BinanceAPIError as e:
            return OrderResult.fail(str(e))
        except Exception as e:
            return OrderResult.fail(f"下单失败: {e}")
    
    async def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> bool:
        """取消订单"""
        if not symbol:
            logger.error("Binance 取消订单需要指定 symbol")
            return False
        
        binance_symbol = SymbolConverter.to_binance(symbol)
        
        try:
            await self._request(
                "DELETE",
                "/api/v3/order",
                params={"symbol": binance_symbol, "orderId": order_id},
                signed=True,
                weight=1
            )
            return True
        except Exception as e:
            logger.error(f"取消订单失败: {e}")
            return False
    
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """取消所有订单"""
        if not symbol:
            logger.error("Binance 批量取消需要指定 symbol")
            return False
        
        binance_symbol = SymbolConverter.to_binance(symbol)
        
        try:
            await self._request(
                "DELETE",
                "/api/v3/openOrders",
                params={"symbol": binance_symbol},
                signed=True,
                weight=1
            )
            return True
        except Exception as e:
            logger.error(f"批量取消失败: {e}")
            return False
    
    async def get_order(self, order_id: str, symbol: str) -> Optional[Order]:
        """查询订单状态"""
        binance_symbol = SymbolConverter.to_binance(symbol)
        
        try:
            data = await self._request(
                "GET",
                "/api/v3/order",
                params={"symbol": binance_symbol, "orderId": order_id},
                signed=True,
                weight=2
            )
            
            # 状态映射
            status_map = {
                "NEW": OrderStatus.OPEN,
                "PARTIALLY_FILLED": OrderStatus.OPEN,
                "FILLED": OrderStatus.FILLED,
                "CANCELED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.FAILED,
                "EXPIRED": OrderStatus.CANCELLED,
            }
            
            return Order(
                order_id=str(data["orderId"]),
                symbol=symbol,
                side=OrderSide.BUY if data["side"] == "BUY" else OrderSide.SELL,
                order_type=OrderType.LIMIT,
                price=float(data["price"]),
                size=float(data["origQty"]),
                status=status_map.get(data["status"], OrderStatus.PENDING),
                filled_size=float(data["executedQty"]),
                created_at=datetime.fromtimestamp(data["time"] / 1000)
            )
            
        except Exception as e:
            logger.error(f"查询订单失败: {e}")
            return None
    
    # ==================== 辅助方法 ====================
    
    def _format_quantity(self, binance_symbol: str, quantity: float) -> str:
        """格式化数量 (符合交易所精度规则)"""
        info = self._symbol_info.get(binance_symbol, {})
        
        # 从 filters 获取精度
        for f in info.get("filters", []):
            if f.get("filterType") == "LOT_SIZE":
                step_size = float(f.get("stepSize", "0.00001"))
                precision = len(str(step_size).rstrip('0').split('.')[-1])
                return f"{quantity:.{precision}f}"
        
        return f"{quantity:.8f}"
    
    def _format_price(self, binance_symbol: str, price: float) -> str:
        """格式化价格 (符合交易所精度规则)"""
        info = self._symbol_info.get(binance_symbol, {})
        
        for f in info.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                tick_size = float(f.get("tickSize", "0.01"))
                precision = len(str(tick_size).rstrip('0').split('.')[-1])
                return f"{price:.{precision}f}"
        
        return f"{price:.8f}"
    
    # ==================== 实时数据流 ====================
    
    async def stream_orderbook(self, symbol: str, depth: int = 20) -> AsyncIterator[OrderBook]:
        """
        订阅订单簿实时更新
        
        使用 Binance WebSocket 深度流，提供实时订单簿快照。
        
        Args:
            symbol: 交易对 (统一格式 "ETH-USDT")
            depth: 深度 (5, 10, 20)
            
        Yields:
            OrderBook 快照
        """
        from connectors.binance.ws_streams import BinanceWebSocketManager
        
        ws_manager = BinanceWebSocketManager(testnet=self._testnet)
        
        try:
            if not await ws_manager.connect():
                return
            
            async for orderbook in ws_manager.stream_depth(symbol, depth):
                yield orderbook
                
        finally:
            await ws_manager.disconnect()
    
    async def stream_trades(self, symbol: str) -> AsyncIterator[Trade]:
        """
        订阅实时成交流
        
        Args:
            symbol: 交易对 (统一格式)
            
        Yields:
            Trade 成交记录
        """
        from connectors.binance.ws_streams import BinanceWebSocketManager
        
        ws_manager = BinanceWebSocketManager(testnet=self._testnet)
        
        try:
            if not await ws_manager.connect():
                return
            
            async for trade in ws_manager.stream_agg_trades(symbol):
                yield trade
                
        finally:
            await ws_manager.disconnect()
    
    async def get_user_data_stream(self):
        """
        获取用户数据流管理器
        
        用于订阅订单更新、账户余额变化等私有数据。
        
        Returns:
            BinanceUserDataStream 实例
            
        使用示例:
        ```python
        user_stream = await connector.get_user_data_stream()
        await user_stream.start()
        
        async for update in user_stream.stream_order_updates():
            print(f"订单更新: {update}")
        ```
        """
        from connectors.binance.ws_streams import BinanceUserDataStream
        
        if not self._auth:
            raise TradingError("需要 API 认证才能使用用户数据流")
        
        return BinanceUserDataStream(
            auth=self._auth,
            testnet=self._testnet
        )

