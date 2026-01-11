"""
Binance WebSocket 数据流

实现订单簿、成交流和用户数据的实时订阅。
支持自动重连和心跳保活。

Binance WebSocket 文档:
- Spot: wss://stream.binance.com:9443/ws
- Testnet: wss://testnet.binance.vision/ws
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator, Optional, Callable, Dict, Any, List
from dataclasses import dataclass

import aiohttp

from connectors.base import (
    OrderBook,
    OrderBookLevel,
    Trade,
    OrderSide,
)
from connectors.binance.auth import SymbolConverter

logger = logging.getLogger(__name__)


# ==================== WebSocket URL ====================

WS_MAINNET = "wss://stream.binance.com:9443/ws"
WS_TESTNET = "wss://testnet.binance.vision/ws"


# ==================== 用户数据事件 ====================

@dataclass
class BinanceOrderUpdate:
    """订单更新事件"""
    event_type: str  # "executionReport"
    symbol: str
    order_id: int
    client_order_id: str
    side: str
    order_type: str
    status: str
    price: float
    quantity: float
    filled_quantity: float
    timestamp: datetime


@dataclass
class BinanceTradeUpdate:
    """账户成交事件"""
    symbol: str
    order_id: int
    trade_id: int
    price: float
    quantity: float
    commission: float
    commission_asset: str
    is_buyer: bool
    is_maker: bool
    timestamp: datetime


# ==================== WebSocket 管理器 ====================

class BinanceWebSocketManager:
    """
    Binance WebSocket 连接管理器
    
    功能:
    - 多流复用单连接
    - 自动重连
    - 心跳保活
    - 订单簿本地维护
    - SOCKS5 代理支持
    """
    
    def __init__(self, testnet: bool = False, proxy_url: str = None):
        self._base_url = WS_TESTNET if testnet else WS_MAINNET
        self._testnet = testnet
        self._proxy_url = proxy_url
        
        # 连接状态
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        
        # 订阅管理
        self._subscriptions: Dict[str, Callable] = {}
        self._message_handlers: Dict[str, Callable] = {}
        
        # 订单簿本地缓存 (symbol -> {bids: {price: qty}, asks: {price: qty}})
        self._orderbooks: Dict[str, Dict[str, Dict[str, float]]] = {}
        
        # 重连配置
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0
    
    async def connect(self) -> bool:
        """建立 WebSocket 连接"""
        try:
            # 如果使用代理
            if self._proxy_url:
                try:
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(self._proxy_url)
                    self._session = aiohttp.ClientSession(connector=connector)
                    logger.info(f"使用代理: {self._proxy_url.split('@')[-1] if '@' in self._proxy_url else self._proxy_url}")
                except ImportError:
                    logger.warning("aiohttp_socks 未安装，不使用代理")
                    self._session = aiohttp.ClientSession()
            else:
                self._session = aiohttp.ClientSession()
            
            self._ws = await self._session.ws_connect(
                self._base_url,
                heartbeat=30,
                receive_timeout=60,
            )
            self._running = True
            logger.info(f"Binance WebSocket 已连接: {self._base_url}")
            return True
        except Exception as e:
            logger.error(f"WebSocket 连接失败: {e}")
            return False
    
    async def disconnect(self) -> None:
        """断开连接"""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        logger.info("Binance WebSocket 已断开")
    
    async def subscribe(self, stream: str) -> bool:
        """
        订阅数据流
        
        Args:
            stream: 流名称 (如 "ethusdt@depth", "ethusdt@trade")
        """
        if not self._ws:
            return False
        
        msg = {
            "method": "SUBSCRIBE",
            "params": [stream],
            "id": hash(stream) % 1000000
        }
        
        await self._ws.send_json(msg)
        logger.info(f"订阅: {stream}")
        return True
    
    async def unsubscribe(self, stream: str) -> bool:
        """取消订阅"""
        if not self._ws:
            return False
        
        msg = {
            "method": "UNSUBSCRIBE",
            "params": [stream],
            "id": hash(stream) % 1000000
        }
        
        await self._ws.send_json(msg)
        return True
    
    async def _receive_loop(self) -> AsyncIterator[Dict[str, Any]]:
        """消息接收循环"""
        while self._running and self._ws:
            try:
                msg = await self._ws.receive()
                
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    yield data
                    
                elif msg.type == aiohttp.WSMsgType.PING:
                    await self._ws.pong(msg.data)
                    
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning(f"WebSocket 关闭: {msg.type}")
                    break
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"WebSocket 接收错误: {e}")
                break
    
    # ==================== 订单簿流 ====================
    
    async def stream_depth(
        self, 
        symbol: str,
        depth: int = 20
    ) -> AsyncIterator[OrderBook]:
        """
        订阅订单簿深度流
        
        Binance 提供两种深度流:
        - @depth: 增量更新 (1000ms)
        - @depth@100ms: 增量更新 (100ms)
        - @depth5, @depth10, @depth20: 快照
        
        Args:
            symbol: 交易对 (统一格式 "ETH-USDT")
            depth: 深度 (5, 10, 20)
            
        Yields:
            OrderBook 快照
        """
        binance_symbol = SymbolConverter.to_binance(symbol).lower()
        stream = f"{binance_symbol}@depth{depth}@100ms"
        
        if not await self.subscribe(stream):
            return
        
        try:
            async for data in self._receive_loop():
                # 跳过订阅确认消息
                if "result" in data:
                    continue
                
                # 解析深度数据
                if "bids" in data and "asks" in data:
                    bids = [
                        OrderBookLevel(price=float(p), size=float(s))
                        for p, s in data.get("bids", [])
                    ]
                    asks = [
                        OrderBookLevel(price=float(p), size=float(s))
                        for p, s in data.get("asks", [])
                    ]
                    
                    yield OrderBook(
                        symbol=symbol,
                        timestamp=datetime.now(),
                        bids=bids,
                        asks=asks
                    )
        finally:
            await self.unsubscribe(stream)
    
    async def stream_depth_diff(
        self,
        symbol: str
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        订阅订单簿增量更新流
        
        适用于需要维护完整订单簿的场景。
        需要配合 REST API 获取初始快照。
        
        Yields:
            增量更新数据
        """
        binance_symbol = SymbolConverter.to_binance(symbol).lower()
        stream = f"{binance_symbol}@depth@100ms"
        
        if not await self.subscribe(stream):
            return
        
        try:
            async for data in self._receive_loop():
                if "result" in data:
                    continue
                
                if "e" in data and data["e"] == "depthUpdate":
                    yield {
                        "first_update_id": data["U"],
                        "last_update_id": data["u"],
                        "bids": [(float(p), float(s)) for p, s in data.get("b", [])],
                        "asks": [(float(p), float(s)) for p, s in data.get("a", [])],
                        "timestamp": datetime.fromtimestamp(data["E"] / 1000),
                    }
        finally:
            await self.unsubscribe(stream)
    
    # ==================== 成交流 ====================
    
    async def stream_trades(self, symbol: str) -> AsyncIterator[Trade]:
        """
        订阅实时成交流
        
        Args:
            symbol: 交易对 (统一格式)
            
        Yields:
            Trade 成交记录
        """
        binance_symbol = SymbolConverter.to_binance(symbol).lower()
        stream = f"{binance_symbol}@trade"
        
        if not await self.subscribe(stream):
            return
        
        try:
            async for data in self._receive_loop():
                if "result" in data:
                    continue
                
                if "e" in data and data["e"] == "trade":
                    yield Trade(
                        trade_id=str(data["t"]),
                        symbol=symbol,
                        price=float(data["p"]),
                        size=float(data["q"]),
                        side=OrderSide.SELL if data["m"] else OrderSide.BUY,
                        timestamp=datetime.fromtimestamp(data["T"] / 1000)
                    )
        finally:
            await self.unsubscribe(stream)
    
    async def stream_agg_trades(self, symbol: str) -> AsyncIterator[Trade]:
        """
        订阅聚合成交流 (推荐)
        
        相比 @trade，@aggTrade 聚合了同一价格的连续成交。
        """
        binance_symbol = SymbolConverter.to_binance(symbol).lower()
        stream = f"{binance_symbol}@aggTrade"
        
        if not await self.subscribe(stream):
            return
        
        try:
            async for data in self._receive_loop():
                if "result" in data:
                    continue
                
                if "e" in data and data["e"] == "aggTrade":
                    yield Trade(
                        trade_id=str(data["a"]),
                        symbol=symbol,
                        price=float(data["p"]),
                        size=float(data["q"]),
                        side=OrderSide.SELL if data["m"] else OrderSide.BUY,
                        timestamp=datetime.fromtimestamp(data["T"] / 1000)
                    )
        finally:
            await self.unsubscribe(stream)


# ==================== 用户数据流 ====================

class BinanceUserDataStream:
    """
    Binance 用户数据流
    
    订阅订单更新、账户余额变化等私有数据。
    需要通过 REST API 获取 listenKey。
    """
    
    def __init__(
        self,
        auth,  # BinanceAuth
        testnet: bool = False
    ):
        self._auth = auth
        self._testnet = testnet
        self._base_url = WS_TESTNET if testnet else WS_MAINNET
        self._rest_url = (
            "https://testnet.binance.vision" if testnet 
            else "https://api.binance.com"
        )
        
        self._listen_key: Optional[str] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        
        # 心跳任务
        self._keepalive_task: Optional[asyncio.Task] = None
    
    async def start(self) -> bool:
        """启动用户数据流"""
        try:
            self._session = aiohttp.ClientSession()
            
            # 获取 listenKey
            self._listen_key = await self._get_listen_key()
            if not self._listen_key:
                return False
            
            # 连接 WebSocket
            ws_url = f"{self._base_url}/{self._listen_key}"
            self._ws = await self._session.ws_connect(
                ws_url,
                heartbeat=30,
                receive_timeout=60,
            )
            
            self._running = True
            
            # 启动 keepalive (每 30 分钟续期)
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            
            logger.info("Binance 用户数据流已启动")
            return True
            
        except Exception as e:
            logger.error(f"用户数据流启动失败: {e}")
            return False
    
    async def stop(self) -> None:
        """停止用户数据流"""
        self._running = False
        
        if self._keepalive_task:
            self._keepalive_task.cancel()
        
        if self._listen_key:
            await self._close_listen_key()
        
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        
        logger.info("Binance 用户数据流已停止")
    
    async def _get_listen_key(self) -> Optional[str]:
        """获取 listenKey"""
        try:
            async with self._session.post(
                f"{self._rest_url}/api/v3/userDataStream",
                headers={"X-MBX-APIKEY": self._auth.api_key}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("listenKey")
        except Exception as e:
            logger.error(f"获取 listenKey 失败: {e}")
        return None
    
    async def _keepalive_listen_key(self) -> bool:
        """续期 listenKey"""
        try:
            async with self._session.put(
                f"{self._rest_url}/api/v3/userDataStream",
                headers={"X-MBX-APIKEY": self._auth.api_key},
                params={"listenKey": self._listen_key}
            ) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"续期 listenKey 失败: {e}")
        return False
    
    async def _close_listen_key(self) -> None:
        """关闭 listenKey"""
        try:
            await self._session.delete(
                f"{self._rest_url}/api/v3/userDataStream",
                headers={"X-MBX-APIKEY": self._auth.api_key},
                params={"listenKey": self._listen_key}
            )
        except Exception:
            pass
    
    async def _keepalive_loop(self) -> None:
        """keepalive 循环 (每 30 分钟)"""
        while self._running:
            await asyncio.sleep(30 * 60)  # 30 分钟
            await self._keepalive_listen_key()
    
    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """
        接收用户数据事件
        
        事件类型:
        - executionReport: 订单更新
        - outboundAccountPosition: 账户余额更新
        """
        while self._running and self._ws:
            try:
                msg = await self._ws.receive()
                
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    yield data
                    
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("用户数据流断开")
                    break
                    
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"用户数据流错误: {e}")
                break
    
    async def stream_order_updates(self) -> AsyncIterator[BinanceOrderUpdate]:
        """
        订阅订单更新
        
        Yields:
            BinanceOrderUpdate 订单状态变化
        """
        async for data in self.stream():
            if data.get("e") == "executionReport":
                yield BinanceOrderUpdate(
                    event_type=data["e"],
                    symbol=SymbolConverter.from_binance(data["s"]),
                    order_id=data["i"],
                    client_order_id=data["c"],
                    side=data["S"],
                    order_type=data["o"],
                    status=data["X"],
                    price=float(data["p"]),
                    quantity=float(data["q"]),
                    filled_quantity=float(data["z"]),
                    timestamp=datetime.fromtimestamp(data["T"] / 1000)
                )
    
    async def stream_trade_updates(self) -> AsyncIterator[BinanceTradeUpdate]:
        """
        订阅账户成交更新
        
        Yields:
            BinanceTradeUpdate 成交记录
        """
        async for data in self.stream():
            if data.get("e") == "executionReport" and data.get("x") == "TRADE":
                yield BinanceTradeUpdate(
                    symbol=SymbolConverter.from_binance(data["s"]),
                    order_id=data["i"],
                    trade_id=data["t"],
                    price=float(data["L"]),
                    quantity=float(data["l"]),
                    commission=float(data["n"]),
                    commission_asset=data["N"],
                    is_buyer=data["S"] == "BUY",
                    is_maker=data["m"],
                    timestamp=datetime.fromtimestamp(data["T"] / 1000)
                )
