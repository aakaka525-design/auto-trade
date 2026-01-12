"""
Binance WebSocket 连接管理模块

提供 WebSocket 连接建立、重连、速率限制等功能。
"""
import asyncio
import aiohttp
import json
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import List, Optional, Callable, Any

logger = logging.getLogger(__name__)

# WebSocket URL
WS_SPOT_URL = "wss://stream.binance.com:9443/stream"
# 合约使用备用数据流地址 (可能更稳定)
WS_FUTURES_URL = "wss://fstream.binance.com/stream"
WS_FUTURES_URL_ALT = "wss://fstream1.binance.com/stream"  # 备用


class BinanceWebSocketManager:
    """
    Binance WebSocket 连接管理器
    
    功能:
    - 多连接管理
    - 自动重连 (有次数限制)
    - 连接速率限制 (每 IP 每 5 分钟 ≤300 次)
    - 代理支持
    """
    
    def __init__(
        self,
        on_message: Callable[[dict, str], Any] = None,
        max_reconnect_attempts: int = 10,
        rate_limit_window: int = 300,
        rate_limit_max: int = 280,
    ):
        """
        Args:
            on_message: 消息回调 (data, market)
            max_reconnect_attempts: 最大重连次数
            rate_limit_window: 速率限制窗口 (秒)
            rate_limit_max: 窗口内最大连接数
        """
        self.on_message = on_message
        self.max_reconnect_attempts = max_reconnect_attempts
        self.rate_limit_window = rate_limit_window
        self.rate_limit_max = rate_limit_max
        
        self._sessions: List[aiohttp.ClientSession] = []
        self._websockets: List[aiohttp.ClientWebSocketResponse] = []
        self._running = False
        self._connection_timestamps: deque = deque(maxlen=300)
        
        # 统计
        self.stats = {
            "connections": 0,
            "messages": 0,
            "reconnects": 0,
        }
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    def start(self):
        """标记为运行中"""
        self._running = True
    
    def stop(self):
        """标记为停止"""
        self._running = False
    
    async def wait_for_rate_limit(self) -> None:
        """等待直到可以建立新连接"""
        now = datetime.now()
        
        # 清理过期记录
        cutoff = now - timedelta(seconds=self.rate_limit_window)
        while self._connection_timestamps and self._connection_timestamps[0] < cutoff:
            self._connection_timestamps.popleft()
        
        # 检查是否超限
        if len(self._connection_timestamps) >= self.rate_limit_max:
            oldest = self._connection_timestamps[0]
            wait_seconds = (oldest + timedelta(seconds=self.rate_limit_window) - now).total_seconds()
            wait_seconds = max(1, min(wait_seconds, 60))
            
            logger.warning(f"⏳ 连接速率限制: 已达 {len(self._connection_timestamps)}/{self.rate_limit_max}，等待 {wait_seconds:.0f}s")
            await asyncio.sleep(wait_seconds)
        
        self._connection_timestamps.append(now)
    
    async def connect(
        self,
        symbols: List[str],
        batch_id: int,
        market: str = "spot"
    ) -> Optional[aiohttp.ClientWebSocketResponse]:
        """
        建立 WebSocket 连接
        
        Args:
            symbols: 交易对列表
            batch_id: 批次 ID
            market: "spot" 或 "futures"
            
        Returns:
            WebSocket 连接或 None
        """
        ws_url = WS_FUTURES_URL if market == "futures" else WS_SPOT_URL
        market_label = "合约" if market == "futures" else "现货"
        
        # 构建订阅 URL
        streams = []
        for s in symbols:
            streams.append(f"{s}@aggTrade")
            streams.append(f"{s}@depth20")
        stream_param = "/".join(streams)
        url = f"{ws_url}?streams={stream_param}"
        
        # 重试连接
        max_retries = 3
        for retry in range(max_retries):
            if not self._running:
                return None
            
            try:
                await self.wait_for_rate_limit()
                
                from connectors.proxy_rotator import create_session_with_proxy
                session, proxy_ip = await create_session_with_proxy()
                
                logger.info(f"🔄 {market_label}连接 #{batch_id} 尝试 (代理: {proxy_ip})...")
                
                # Binance 每 20 秒发送 PING，所以心跳间隔设为 15 秒
                ws = await session.ws_connect(url, heartbeat=15, timeout=30)
                
                self._sessions.append(session)
                self._websockets.append(ws)
                self.stats["connections"] += 1
                
                logger.info(f"✅ {market_label}连接 #{batch_id} 成功 | 代理: {proxy_ip} | {len(symbols)} 交易对")
                return ws
                
            except Exception as e:
                logger.warning(f"❌ {market_label}连接 #{batch_id} 失败 (尝试 {retry + 1}/{max_retries}): {e}")
                if 'session' in locals() and session and not session.closed:
                    await session.close()
                await asyncio.sleep(1)
        
        return None
    
    async def handle_connection(
        self,
        symbols: List[str],
        batch_id: int,
        market: str = "spot"
    ) -> None:
        """
        处理单个连接 (带自动重连)
        """
        market_label = "合约" if market == "futures" else "现货"
        reconnect_delay = 1.0
        max_reconnect_delay = 60.0
        reconnect_count = 0
        
        while self._running:
            if reconnect_count >= self.max_reconnect_attempts:
                logger.error(f"❌ {market_label}连接 #{batch_id} 已达最大重连次数 ({self.max_reconnect_attempts})，放弃")
                break
            
            # 显示重连信息
            if reconnect_count > 0:
                logger.info(f"🔄 {market_label}连接 #{batch_id} 正在重连... (第 {reconnect_count} 次)")
            
            ws = await self.connect(symbols, batch_id, market)
            
            if not ws:
                reconnect_count += 1
                self.stats["reconnects"] += 1
                logger.warning(f"❌ {market_label}连接 #{batch_id} 重连失败，将在 {reconnect_delay:.0f}s 后再试 ({reconnect_count}/{self.max_reconnect_attempts})")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
                continue
            
            # 连接成功，重置计数
            if reconnect_count > 0:
                logger.info(f"✅ {market_label}连接 #{batch_id} 重连成功!")
            reconnect_count = 0
            reconnect_delay = 1.0
            
            # 消息循环
            try:
                while self._running:
                    try:
                        msg = await ws.receive(timeout=30)  # 添加超时
                        
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            self.stats["messages"] += 1
                            
                            if self.on_message and "data" in data:
                                await self.on_message(data["data"], market)
                        
                        elif msg.type == aiohttp.WSMsgType.PING:
                            # 收到 PING，必须回复 PONG
                            await ws.pong(msg.data)
                        
                        elif msg.type == aiohttp.WSMsgType.PONG:
                            # 正常的心跳响应，忽略
                            pass
                        
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"⚠️ {market_label}连接 #{batch_id} 断开，准备重连...")
                            break
                        
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            logger.warning(f"⚠️ {market_label}连接 #{batch_id} 收到关闭请求: {msg.data}")
                            break
                            
                    except asyncio.TimeoutError:
                        # 30秒没消息，发送 PING 保活
                        try:
                            await ws.ping()
                        except:
                            break
                    except Exception as e:
                        logger.error(f"{market_label}连接 #{batch_id} 消息处理错误: {e}")
                        break
                        
            finally:
                # 清理
                if ws:
                    await ws.close()
                    if ws in self._websockets:
                        self._websockets.remove(ws)
            
            # 断开后准备重连
            if self._running:
                reconnect_count += 1
                self.stats["reconnects"] += 1
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
    
    async def disconnect_all(self) -> None:
        """断开所有连接"""
        self._running = False
        
        for ws in self._websockets:
            try:
                await ws.close()
            except:
                pass
        
        for session in self._sessions:
            try:
                await session.close()
            except:
                pass
        
        self._websockets.clear()
        self._sessions.clear()
        logger.info("✅ 所有 WebSocket 连接已断开")
