"""
Lighter DEX 连接器

基于 lighter-python SDK 实现 BaseConnector 接口。
增强功能: 指数退避重试、API 限流、Nonce 管理、健康检查。
"""
import asyncio
import logging
from typing import AsyncIterator, Optional
from datetime import datetime

from connectors.base import (
    BaseConnector, 
    OrderBook, OrderBookLevel, Candlestick, Trade,
    Order, OrderResult, Position, AccountInfo,
    OrderSide, OrderType, OrderStatus
)
from connectors.retry import (
    retry_async, RetryConfig, 
    TokenBucketLimiter, NonceManager,
)
from connectors.lighter.ws_orderbook import WebSocketOrderBook, OrderBookSnapshot
from core.exceptions import (
    RPCTimeoutError,
    RateLimitExceededError,
    NonceConflictError,
    ExchangeConnectionError,
    OrderExecutionError,
)

logger = logging.getLogger(__name__)


# ==================== 常量 ====================

# Lighter API 权重
API_WEIGHT = {
    "sendTx": 6,
    "sendTxBatch": 6,
    "nextNonce": 6,
    "orderBook": 300,
    "candlesticks": 300,
    "account": 300,
    "default": 300,
}


# ==================== 连接器实现 ====================

class LighterConnector(BaseConnector):
    """
    Lighter 交易所连接器
    
    封装 lighter-python SDK，提供统一的 BaseConnector 接口。
    
    增强功能:
    - 指数退避重试 (RPC 超时/网络错误)
    - API 限流保护 (令牌桶)
    - Nonce 冲突自动恢复
    - 连接健康检查
    """
    
    # 市场 ID 映射
    MARKET_MAP = {
        "ETH-USDC": 0,
        "BTC-USDC": 1,
        "SOL-USDC": 2,
    }
    
    # 市场精度 (price: 价格小数位, size: 数量小数位)
    MARKET_PRECISION = {
        0: {"price": 2, "size": 4},   # ETH-USDC: price*100, size*10000
        1: {"price": 2, "size": 6},   # BTC-USDC: price*100, size*1000000
        2: {"price": 4, "size": 4},   # SOL-USDC: price*10000, size*10000
    }
    
    # Resolution 映射
    RESOLUTION_MAP = {
        "1m": "1m",
        "5m": "5m",
        "15m": "15m",
        "1h": "1h",
        "4h": "4h",
        "1D": "1D",
    }
    
    def __init__(self, config: dict):
        super().__init__(config)
        
        self.base_url = config.get("base_url", "https://mainnet.zklighter.elliot.ai")
        self.account_index = config.get("account_index")
        self.api_key_index = config.get("api_key_index", 3)
        self.api_private_key = config.get("api_private_key")
        
        self._signer = None
        self._api_client = None
        self._client_order_index = int(datetime.now().timestamp()) % 1000000
        
        # 新增: 限流器和 Nonce 管理器
        # Premium 账户: 24000 权重/60秒 = 400 权重/秒
        self._rate_limiter = TokenBucketLimiter(
            rate=config.get("rate_limit", 400),
            capacity=1000  # 允许短时突发
        )
        self._nonce_manager: Optional[NonceManager] = None
        
        # 重试配置
        self._retry_config = RetryConfig(
            max_retries=config.get("max_retries", 3),
            base_delay=config.get("retry_delay", 0.5),
            max_delay=10.0,
            jitter=True,
        )
        
        # 健康检查状态
        self._last_health_check: Optional[datetime] = None
        self._health_check_interval = 60  # 秒
        
        # 代理配置
        self._proxy = config.get("proxy") or config.get("https_proxy") or config.get("http_proxy")
        if self._proxy:
            import os
            os.environ["HTTP_PROXY"] = self._proxy
            os.environ["HTTPS_PROXY"] = self._proxy
            logger.info(f"使用代理: {self._proxy}")
        
        # WebSocket 订单簿
        self._ws_orderbook: Optional[WebSocketOrderBook] = None
        self._use_ws_orderbook = config.get("use_ws_orderbook", True)
        
        # 订单 ID -> market_id 映射 (用于 cancel_order)
        self._order_market_map: dict[str, int] = {}
    
    # ==================== 连接管理 ====================
    
    async def connect(self) -> bool:
        """初始化 SDK 连接"""
        if self._connected:
            return True
        
        if not self.api_private_key:
            logger.warning("API 私钥未配置，使用只读模式")
            self._connected = True
            return True
        
        try:
            import lighter
            from lighter import SignerClient, ApiClient
            from lighter.configuration import Configuration
            
            # API Client (只读)
            api_config = Configuration(host=self.base_url)
            self._api_client = ApiClient(configuration=api_config)
            
            # Signer Client (读写)
            self._signer = SignerClient(
                url=self.base_url,
                account_index=self.account_index,
                api_private_keys={self.api_key_index: self.api_private_key}
            )
            
            err = self._signer.check_client()
            if err:
                logger.error(f"SignerClient 验证失败: {err}")
                return False
            
            # 初始化 Nonce 管理器
            await self._init_nonce_manager()
            
            # 启动 WebSocket 订单簿
            if self._use_ws_orderbook:
                self._start_ws_orderbook()
            
            self._connected = True
            self._last_health_check = datetime.now()
            logger.info("✅ Lighter 连接器初始化成功")
            return True
            
        except ImportError:
            logger.error("lighter-python SDK 未安装，请运行: pip install lighter-python")
            return False
        except Exception as e:
            logger.error(f"连接失败: {e}")
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        # 停止 WebSocket
        if self._ws_orderbook:
            self._ws_orderbook.stop()
            self._ws_orderbook = None
        
        if self._api_client:
            try:
                await self._api_client.close()
            except Exception:
                pass
        self._signer = None
        self._connected = False
        logger.info("Lighter 连接器已断开")
    
    def _start_ws_orderbook(self) -> None:
        """启动 WebSocket 订单簿"""
        if self._ws_orderbook is not None:
            return
        
        # 提取 host (去掉 scheme)
        ws_host = self.base_url.replace("https://", "").replace("http://", "")
        
        self._ws_orderbook = WebSocketOrderBook(
            market_ids=list(self.MARKET_MAP.values()),
            host=ws_host,
        )
        self._ws_orderbook.start()
        logger.info(f"WebSocket 订单簿已启动: {ws_host}")
    
    async def _init_nonce_manager(self) -> None:
        """初始化 Nonce 管理器"""
        try:
            import lighter
            from lighter.configuration import Configuration
            
            config = Configuration(host=self.base_url)
            async with lighter.ApiClient(configuration=config) as client:
                api = lighter.TransactionApi(client)
                response = await api.next_nonce(
                    account_index=self.account_index,
                    api_key_index=self.api_key_index
                )
                # 注意: 字段名是 nonce 不是 next_nonce
                initial_nonce = int(response.nonce)
            
            self._nonce_manager = NonceManager(initial_nonce)
            logger.info(f"Nonce 管理器初始化: {initial_nonce}")
        except Exception as e:
            logger.warning(f"Nonce 初始化失败，使用默认值: {e}")
            self._nonce_manager = NonceManager(0)
    
    async def health_check(self) -> bool:
        """检查 API 可达性"""
        try:
            import lighter
            from lighter.configuration import Configuration
            
            config = Configuration(host=self.base_url)
            async with lighter.ApiClient(configuration=config) as client:
                api = lighter.RootApi(client)
                await asyncio.wait_for(api.info(), timeout=5.0)
            
            self._last_health_check = datetime.now()
            return True
        except asyncio.TimeoutError:
            logger.warning("健康检查超时")
            return False
        except Exception as e:
            logger.warning(f"健康检查失败: {e}")
            return False
    
    def _get_market_id(self, symbol: str) -> int:
        """获取市场 ID"""
        return self.MARKET_MAP.get(symbol, 0)
    
    def _next_order_index(self) -> int:
        """生成唯一订单索引"""
        self._client_order_index += 1
        return self._client_order_index
    
    # ==================== 带限流的 API 调用 ====================
    
    async def _call_with_rate_limit(
        self, 
        coro_factory, 
        weight: int = 300,
        operation: str = "api_call"
    ):
        """带限流的 API 调用包装"""
        # 获取令牌
        await self._rate_limiter.acquire(weight)
        
        # 带重试执行
        try:
            return await retry_async(
                coro_factory,
                config=self._retry_config,
                on_retry=lambda e, attempt: logger.warning(
                    f"[{operation}] 重试 {attempt + 1}: {type(e).__name__}"
                )
            )
        except asyncio.TimeoutError as e:
            raise RPCTimeoutError(f"{operation} 超时") from e
    
    # ==================== 行情数据 ====================
    
    async def get_orderbook(self, symbol: str, depth: int = 0) -> OrderBook:
        """
        获取订单簿快照 (优先使用 WebSocket)
        
        Args:
            symbol: 交易对
            depth: 返回档数，0 表示全部
        """
        market_id = self._get_market_id(symbol)
        
        # 优先使用 WebSocket 数据
        if self._ws_orderbook and self._ws_orderbook.is_running:
            snapshot = self._ws_orderbook.get_orderbook(market_id)
            if snapshot:
                # 转换格式: {price: size} -> [OrderBookLevel]
                bids = sorted(
                    [(float(p), float(s)) for p, s in snapshot.bids.items()],
                    key=lambda x: x[0], reverse=True
                )
                asks = sorted(
                    [(float(p), float(s)) for p, s in snapshot.asks.items()],
                    key=lambda x: x[0]
                )
                
                # 限制档数
                if depth > 0:
                    bids = bids[:depth]
                    asks = asks[:depth]
                
                return OrderBook(
                    symbol=symbol,
                    timestamp=snapshot.timestamp,
                    bids=[OrderBookLevel(p, s) for p, s in bids],
                    asks=[OrderBookLevel(p, s) for p, s in asks],
                )
        
        # 回退到 REST API
        import lighter
        from lighter.configuration import Configuration
        
        async def _fetch():
            config = Configuration(host=self.base_url)
            async with lighter.ApiClient(configuration=config) as client:
                api = lighter.OrderApi(client)
                return await asyncio.wait_for(
                    api.order_books(market_id=market_id),
                    timeout=5.0
                )
        
        response = await self._call_with_rate_limit(
            _fetch,
            weight=API_WEIGHT["orderBook"],
            operation="get_orderbook"
        )
        
        bids = [
            OrderBookLevel(float(b.price), float(b.size))
            for b in getattr(response, 'bids', []) or []
        ]
        asks = [
            OrderBookLevel(float(a.price), float(a.size))
            for a in getattr(response, 'asks', []) or []
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
    ) -> list[Candlestick]:
        """获取 K 线数据"""
        import time
        import lighter
        from lighter.configuration import Configuration
        
        async def _fetch():
            config = Configuration(host=self.base_url)
            async with lighter.ApiClient(configuration=config) as client:
                api = lighter.CandlestickApi(client)
                
                market_id = self._get_market_id(symbol)
                resolution = self.RESOLUTION_MAP.get(interval, "15m")
                
                now_sec = int(time.time())
                interval_sec = {
                    "1m": 60, "5m": 300, "15m": 900,
                    "1h": 3600, "4h": 14400, "1D": 86400
                }.get(interval, 900)
                
                start_sec = now_sec - (limit * interval_sec)
                
                return await asyncio.wait_for(
                    api.candlesticks(
                        market_id=market_id,
                        resolution=resolution,
                        start_timestamp=start_sec,
                        end_timestamp=now_sec,
                        count_back=limit
                    ),
                    timeout=10.0
                )
        
        response = await self._call_with_rate_limit(
            _fetch,
            weight=API_WEIGHT["candlesticks"],
            operation="get_candlesticks"
        )
        
        result = []
        for c in response.candlesticks:
            vol = getattr(c, 'volume0', None) or getattr(c, 'volume1', 0)
            result.append(Candlestick(
                timestamp=int(c.timestamp),
                open=float(str(c.open).replace(",", "")),
                high=float(str(c.high).replace(",", "")),
                low=float(str(c.low).replace(",", "")),
                close=float(str(c.close).replace(",", "")),
                volume=float(str(vol).replace(",", ""))
            ))
        
        return result
    
    async def get_ticker_price(self, symbol: str) -> float:
        """获取最新价格 (使用 recent_trades API)"""
        import lighter
        from lighter.configuration import Configuration
        
        async def _fetch():
            config = Configuration(host=self.base_url)
            async with lighter.ApiClient(configuration=config) as client:
                api = lighter.OrderApi(client)
                market_id = self._get_market_id(symbol)
                return await asyncio.wait_for(
                    api.recent_trades(market_id=market_id, limit=1),
                    timeout=5.0
                )
        
        try:
            response = await self._call_with_rate_limit(
                _fetch,
                weight=API_WEIGHT["default"],
                operation="get_ticker_price"
            )
            
            if response.trades:
                return float(response.trades[0].price)
        except Exception as e:
            logger.warning(f"获取价格失败: {e}")
        
        return 0.0
    
    # ==================== 账户信息 ====================
    
    async def get_account(self) -> AccountInfo:
        """获取账户信息"""
        import lighter
        from lighter.configuration import Configuration
        
        async def _fetch():
            config = Configuration(host=self.base_url)
            async with lighter.ApiClient(configuration=config) as client:
                api = lighter.AccountApi(client)
                return await asyncio.wait_for(
                    api.account(by="index", value=str(self.account_index)),
                    timeout=5.0
                )
        
        response = await self._call_with_rate_limit(
            _fetch,
            weight=API_WEIGHT["account"],
            operation="get_account"
        )
        
        if not response.accounts:
            return AccountInfo(
                account_id=str(self.account_index),
                available_balance=0.0,
                total_equity=0.0
            )
        
        acct = response.accounts[0]
        
        positions = []
        for p in getattr(acct, 'positions', []) or []:
            size = float(str(getattr(p, 'position', '0')).replace(",", ""))
            if size != 0:
                positions.append(Position(
                    symbol=p.symbol,
                    side=OrderSide.BUY if size > 0 else OrderSide.SELL,
                    size=abs(size),
                    entry_price=float(str(getattr(p, 'avg_entry_price', '0')).replace(",", "")),
                    unrealized_pnl=float(str(getattr(p, 'unrealized_pnl', '0')).replace(",", ""))
                ))
        
        return AccountInfo(
            account_id=str(acct.account_index),
            available_balance=float(str(acct.available_balance).replace(",", "")),
            total_equity=float(str(getattr(acct, 'total_asset_value', acct.available_balance)).replace(",", "")),
            positions=positions
        )
    
    async def get_position(self, symbol: str) -> Optional[Position]:
        """获取指定交易对的持仓"""
        account = await self.get_account()
        for pos in account.positions:
            if pos.symbol.upper() in symbol.upper():
                return pos
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
        reduce_only: bool = False
    ) -> OrderResult:
        """创建订单 (带 Nonce 管理和重试)"""
        if not self._signer:
            return OrderResult.fail("SignerClient 未初始化")
        
        market_id = self._get_market_id(symbol)
        client_order_index = self._next_order_index()
        
        # 使用市场精度常量转换
        precision = self.MARKET_PRECISION.get(market_id, {"price": 2, "size": 4})
        price_multiplier = 10 ** precision["price"]
        size_multiplier = 10 ** precision["size"]
        
        price_int = int((price or 0) * price_multiplier)
        base_amount = int(size * size_multiplier)
        trigger_price = int((stop_price or 0) * price_multiplier)
        
        # 订单类型映射
        type_map = {
            OrderType.LIMIT: self._signer.ORDER_TYPE_LIMIT,
            OrderType.MARKET: self._signer.ORDER_TYPE_MARKET,
            OrderType.STOP_LOSS: self._signer.ORDER_TYPE_STOP_LOSS,
            OrderType.TAKE_PROFIT: self._signer.ORDER_TYPE_TAKE_PROFIT,
        }
        
        is_ask = side == OrderSide.SELL
        
        # 获取限流许可
        await self._rate_limiter.acquire(API_WEIGHT["sendTx"])
        
        # 最多重试 3 次处理 Nonce 冲突
        for attempt in range(3):
            try:
                _, response, error = await asyncio.wait_for(
                    self._signer.create_order(
                        market_index=market_id,
                        client_order_index=client_order_index,
                        base_amount=base_amount,
                        price=price_int,
                        is_ask=is_ask,
                        order_type=type_map.get(order_type, 0),
                        time_in_force=self._signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                        reduce_only=reduce_only,
                        trigger_price=trigger_price,
                    ),
                    timeout=10.0
                )
                
                if error:
                    error_str = str(error)
                    
                    # 检查 Nonce 冲突
                    if "nonce" in error_str.lower():
                        logger.warning(f"Nonce 冲突，重新同步: {error_str}")
                        await self._init_nonce_manager()
                        continue
                    
                    # 检查限流
                    if "429" in error_str or "rate" in error_str.lower():
                        raise RateLimitExceededError(error_str)
                    
                    return OrderResult.fail(error_str)
                
                # 确认 Nonce
                if self._nonce_manager:
                    await self._nonce_manager.confirm(0)  # Signer 内部管理 nonce
                
                logger.info(f"✅ 订单创建成功: {client_order_index}")
                
                # 记录订单到市场映射 (用于 cancel_order)
                self._order_market_map[str(client_order_index)] = market_id
                
                return OrderResult.ok(order_id=str(client_order_index))
                
            except asyncio.TimeoutError:
                logger.error(f"订单超时 (尝试 {attempt + 1}/3)")
                if attempt == 2:
                    return OrderResult.fail("订单创建超时")
                await asyncio.sleep(0.5 * (attempt + 1))
            
            except Exception as e:
                logger.exception(f"订单异常: {e}")
                return OrderResult.fail(str(e))
        
        return OrderResult.fail("订单创建失败 (重试耗尽)")
    
    async def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        if not self._signer:
            return False
        
        await self._rate_limiter.acquire(API_WEIGHT["sendTx"])
        
        try:
            # 查找订单对应的 market_id
            market_id = self._order_market_map.get(order_id, 0)
            
            _, _, error = await asyncio.wait_for(
                self._signer.cancel_order(
                    market_index=market_id,
                    order_index=int(order_id)
                ),
                timeout=10.0
            )
            if error:
                logger.error(f"取消订单失败: {error}")
                return False
            
            # 清理映射
            self._order_market_map.pop(order_id, None)
            return True
        except Exception as e:
            logger.error(f"取消订单异常: {e}")
            return False
    
    async def cancel_all_orders(self, symbol: Optional[str] = None) -> bool:
        """取消所有订单"""
        if not self._signer:
            return False
        
        await self._rate_limiter.acquire(API_WEIGHT["sendTx"])
        
        try:
            import time
            timestamp_ms = int(time.time() * 1000) + 60000  # 1分钟后过期
            
            _, _, error = await asyncio.wait_for(
                self._signer.cancel_all_orders(
                    time_in_force=self._signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                    timestamp_ms=timestamp_ms
                ),
                timeout=10.0
            )
            if error:
                logger.error(f"取消所有订单失败: {error}")
                return False
            
            logger.info("✅ 已取消所有订单")
            return True
        except Exception as e:
            logger.error(f"取消所有订单异常: {e}")
            return False
    
    # ==================== 实时数据流 ====================
    
    async def stream_orderbook(self, symbol: str) -> AsyncIterator[OrderBook]:
        """订阅订单簿实时更新 (带自动重连)"""
        import websockets
        import json
        
        market_id = self._get_market_id(symbol)
        ws_url = self.base_url.replace("https://", "wss://") + "/stream"
        
        max_reconnects = 10
        reconnect_count = 0
        
        while reconnect_count < max_reconnects:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    # 订阅
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": f"order_book/{market_id}"
                    }))
                    
                    reconnect_count = 0  # 成功连接，重置计数
                    logger.info(f"WebSocket 已连接: order_book/{market_id}")
                    
                    async for msg in ws:
                        data = json.loads(msg)
                        if "order_book" in data:
                            ob_data = data["order_book"]
                            
                            bids = [
                                OrderBookLevel(float(b["price"]), float(b["size"]))
                                for b in ob_data.get("bids", [])
                            ]
                            asks = [
                                OrderBookLevel(float(a["price"]), float(a["size"]))
                                for a in ob_data.get("asks", [])
                            ]
                            
                            yield OrderBook(
                                symbol=symbol,
                                timestamp=datetime.now(),
                                bids=bids,
                                asks=asks
                            )
            
            except websockets.exceptions.ConnectionClosed as e:
                reconnect_count += 1
                logger.warning(f"WebSocket 断开，重连 {reconnect_count}/{max_reconnects}: {e}")
                await asyncio.sleep(min(2 ** reconnect_count, 30))
            
            except Exception as e:
                reconnect_count += 1
                logger.error(f"WebSocket 异常: {e}")
                await asyncio.sleep(min(2 ** reconnect_count, 30))
        
        logger.error("WebSocket 重连次数耗尽")
    
    async def stream_trades(self, symbol: str) -> AsyncIterator[Trade]:
        """订阅成交流 (带自动重连)"""
        import websockets
        import json
        
        market_id = self._get_market_id(symbol)
        ws_url = self.base_url.replace("https://", "wss://") + "/stream"
        
        max_reconnects = 10
        reconnect_count = 0
        
        while reconnect_count < max_reconnects:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": f"trade/{market_id}"
                    }))
                    
                    reconnect_count = 0
                    logger.info(f"WebSocket 已连接: trade/{market_id}")
                    
                    async for msg in ws:
                        data = json.loads(msg)
                        for t in data.get("trades", []):
                            yield Trade(
                                trade_id=str(t.get("trade_id", 0)),
                                symbol=symbol,
                                price=float(t["price"]),
                                size=float(t["size"]),
                                side=OrderSide.BUY if t.get("is_ask") else OrderSide.SELL,
                                timestamp=datetime.now()
                            )
            
            except websockets.exceptions.ConnectionClosed as e:
                reconnect_count += 1
                logger.warning(f"WebSocket 断开，重连 {reconnect_count}/{max_reconnects}")
                await asyncio.sleep(min(2 ** reconnect_count, 30))
            
            except Exception as e:
                reconnect_count += 1
                logger.error(f"WebSocket 异常: {e}")
                await asyncio.sleep(min(2 ** reconnect_count, 30))
        
        logger.error("WebSocket 重连次数耗尽")
