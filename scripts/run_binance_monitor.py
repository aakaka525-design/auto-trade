#!/usr/bin/env python3
"""
Binance 全量监控 (多连接架构)

监控所有稳定币交易对 (USDT/USDC/USDE/USD1 等)
使用多个 WebSocket 连接突破 1024 流限制

架构:
- 每个连接最多 500 个流 (250 交易对 x 2 流)
- 自动分批建立多个连接
- 统一的大单和价格检测
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 加载 .env 环境变量 (必须在其他导入之前)
from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from enum import Enum
from collections import deque

import aiohttp

from config import settings
from connectors.binance.auth import SymbolConverter

# 配置日志 (控制台 + 文件)
log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)

# 创建 logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
))

# 文件处理器 (按天轮转)
from logging.handlers import TimedRotatingFileHandler
file_handler = TimedRotatingFileHandler(
    log_dir / "binance_monitor.log",
    when="midnight",
    interval=1,
    backupCount=7,
    encoding="utf-8"
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))

logger.addHandler(console_handler)
logger.addHandler(file_handler)


# ==================== 常量 ====================

WS_COMBINED_URL = "wss://stream.binance.com:9443/stream"

# 稳定币后缀列表
STABLECOIN_SUFFIXES = ['USDT', 'USDC', 'USDE', 'USD1', 'TUSD', 'BUSD', 'FDUSD']

# 每个连接最大交易对数 (降到 30 减少数据量，提高连接稳定性)
# 合约市场数据量大，需要更少的交易对/连接
MAX_SYMBOLS_PER_CONNECTION = 30


# ==================== Telegram 分级通知 ====================

class AlertLevel(str, Enum):
    """告警级别"""
    LOW = "low"       # 普通大单 (日志记录，可选推送)
    MEDIUM = "medium" # 中等冲击 (推送到普通频道)
    HIGH = "high"     # 极端行情 (推送到紧急频道)


class TelegramNotifier:
    """Telegram 分级通知 (支持不同 Bot 区分告警级别)"""
    
    def __init__(
        self,
        # 默认 Bot (普通告警)
        token: str = "",
        chat_id: str = "",
        # 紧急 Bot (极端行情)
        urgent_token: str = "",
        urgent_chat_id: str = "",
        rate_limit: int = 30
    ):
        self.token = token
        self.chat_id = chat_id
        self.urgent_token = urgent_token or token  # 未配置则使用默认
        self.urgent_chat_id = urgent_chat_id or chat_id
        self.rate_limit = rate_limit
        self._last_send_times: deque = deque(maxlen=rate_limit)
    
    async def send(self, message: str, level: str = AlertLevel.MEDIUM) -> bool:
        """
        发送消息 (根据级别选择 Bot)
        
        Args:
            message: 消息内容
            level: 告警级别 (low/medium/high)
        """
        # 选择 Bot
        if level == AlertLevel.HIGH:
            token = self.urgent_token
            chat_id = self.urgent_chat_id
        else:
            token = self.token
            chat_id = self.chat_id
        
        if not token or not chat_id:
            return False
        
        # 速率限制
        now = datetime.now()
        while self._last_send_times and (now - self._last_send_times[0]).seconds > 60:
            self._last_send_times.popleft()
        
        if len(self._last_send_times) >= self.rate_limit:
            return False
        
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = {"chat_id": chat_id, "text": message, "parse_mode": "HTML"}
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as resp:
                    if resp.status == 200:
                        self._last_send_times.append(now)
                        return True
            return False
        except Exception:
            return False


# ==================== 获取交易对 ====================

async def get_spot_symbols() -> List[dict]:
    """获取现货稳定币交易对"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.binance.com/api/v3/ticker/24hr") as resp:
                if resp.status != 200:
                    return []
                
                data = await resp.json()
                pairs = []
                for item in data:
                    symbol = item['symbol']
                    for suffix in STABLECOIN_SUFFIXES:
                        if symbol.endswith(suffix):
                            pairs.append({
                                'symbol': symbol.lower(),
                                'volume': float(item.get('quoteVolume', 0)),
                                'market': 'spot'
                            })
                            break
                
                logger.info(f"现货: 找到 {len(pairs)} 个稳定币交易对")
                return pairs
    except Exception as e:
        logger.error(f"获取现货交易对失败: {e}")
        return []


async def get_futures_symbols() -> List[dict]:
    """获取 U 本位合约交易对"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr") as resp:
                if resp.status != 200:
                    return []
                
                data = await resp.json()
                pairs = []
                for item in data:
                    symbol = item['symbol']
                    # 合约大多是 USDT 结尾
                    if symbol.endswith('USDT') or symbol.endswith('USDC'):
                        pairs.append({
                            'symbol': symbol.lower(),
                            'volume': float(item.get('quoteVolume', 0)),
                            'market': 'futures'
                        })
                
                logger.info(f"合约: 找到 {len(pairs)} 个交易对")
                return pairs
    except Exception as e:
        logger.error(f"获取合约交易对失败: {e}")
        return []


async def get_all_symbols() -> tuple[List[dict], List[dict]]:
    """获取所有交易对 (现货 + 合约)"""
    spot_pairs, futures_pairs = await asyncio.gather(
        get_spot_symbols(),
        get_futures_symbols()
    )
    
    # 按成交量排序
    spot_pairs.sort(key=lambda x: x['volume'], reverse=True)
    futures_pairs.sort(key=lambda x: x['volume'], reverse=True)
    
    return spot_pairs, futures_pairs


# ==================== 多连接监控器 ====================

class BinanceMultiConnectionMonitor:
    """
    Binance 多连接监控器 (VWAP 滑点检测 + 分级告警)
    
    核心逻辑:
    - 买单吃 Ask (卖盘)，卖单吃 Bid (买盘)
    - 模拟成交计算 VWAP 滑点
    - 滑点超过阈值触发分级报警
    
    告警级别:
    - LOW: 滑点 >= slippage_low (日志记录)
    - MEDIUM: 滑点 >= slippage_medium (普通 Bot)
    - HIGH: 滑点 >= slippage_high (紧急 Bot)
    """
    
    def __init__(self):
        # 分级滑点阈值
        self.slippage_low = getattr(settings, 'SLIPPAGE_THRESHOLD_LOW', 0.5)      # 0.5%
        self.slippage_medium = getattr(settings, 'SLIPPAGE_THRESHOLD_MED', 2.0)   # 2%
        self.slippage_high = getattr(settings, 'SLIPPAGE_THRESHOLD_HIGH', 10.0)   # 10%
        
        # 兼容旧配置 (单一阈值)
        old_threshold = getattr(settings, 'SLIPPAGE_THRESHOLD', None)
        if old_threshold and not hasattr(settings, 'SLIPPAGE_THRESHOLD_LOW'):
            self.slippage_low = old_threshold
            self.slippage_medium = old_threshold * 2
            self.slippage_high = old_threshold * 5
        
        # 最低金额阈值
        self.min_order_spot = getattr(settings, 'MIN_ORDER_VALUE_SPOT', 50000.0)
        self.min_order_futures = getattr(settings, 'MIN_ORDER_VALUE_FUTURES', 20000.0)
        self.orderbook_depth = getattr(settings, 'ORDERBOOK_DEPTH', 50)
        self.skip_top_levels = getattr(settings, 'SKIP_TOP_LEVELS', 1)
        
        # 监控开关
        self.monitor_spot = getattr(settings, 'BINANCE_MONITOR_SPOT', True)
        self.monitor_futures = getattr(settings, 'BINANCE_MONITOR_FUTURES', True)
        
        # 订单簿缓存
        self.orderbook_bids: Dict[str, List[tuple]] = {}  # 降序
        self.orderbook_asks: Dict[str, List[tuple]] = {}  # 升序
        
        # 价格缓存
        self.price_cache: Dict[str, float] = {}
        
        # 冷却控制
        self.alert_cooldown: Dict[str, datetime] = {}
        self.cooldown_seconds = getattr(settings, 'PRICE_COOLDOWN', 120)
        
        # 统计 (分级)
        self.stats = {
            'connections': 0,
            'trades': 0,
            'depth_updates': 0,
            'alerts_low': 0,
            'alerts_medium': 0,
            'alerts_high': 0,
        }
        self.start_time = datetime.now()
        
        # Telegram 分级通知
        self.notifier = TelegramNotifier(
            token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
            urgent_token=getattr(settings, 'TELEGRAM_URGENT_BOT_TOKEN', ''),
            urgent_chat_id=getattr(settings, 'TELEGRAM_URGENT_CHAT_ID', ''),
        )
        
        # 连接管理
        self._sessions: List[aiohttp.ClientSession] = []
        self._websockets: List[aiohttp.ClientWebSocketResponse] = []
        self._running = False
    
    def get_alert_level(self, slippage: float) -> Optional[str]:
        """根据滑点返回告警级别"""
        if slippage >= self.slippage_high:
            return AlertLevel.HIGH
        elif slippage >= self.slippage_medium:
            return AlertLevel.MEDIUM
        elif slippage >= self.slippage_low:
            return AlertLevel.LOW
        return None
    
    def get_min_order(self, market: str) -> float:
        """获取最低金额阈值"""
        return self.min_order_spot if market == "spot" else self.min_order_futures
    
    def calculate_slippage(self, cache_key: str, order_value: float, is_buy: bool) -> tuple[float, float]:
        """
        模拟成交计算滑点
        
        Args:
            cache_key: 缓存键 (market:symbol)
            order_value: 订单金额 (USD)
            is_buy: 是否买单 (买单吃Ask，卖单吃Bid)
        
        Returns:
            (滑点%, VWAP价格)
        """
        # 买单吃卖盘 (Ask)，卖单吃买盘 (Bid)
        if is_buy:
            orderbook = self.orderbook_asks.get(cache_key, [])
        else:
            orderbook = self.orderbook_bids.get(cache_key, [])
        
        if not orderbook:
            return 0, 0
        
        # 最小档位数要求 (数据不充分时跳过)
        min_levels = 10
        skip = self.skip_top_levels
        if len(orderbook) < min_levels + skip:
            return 0, 0  # 档位不足，数据不充分
        
        current_price = orderbook[skip][0]  # 跳过前 N 档后的第一档价格
        
        # 模拟成交
        remaining_value = order_value
        total_cost = 0
        total_qty = 0
        
        for i, (price, size) in enumerate(orderbook):
            if i < skip:  # 跳过前 N 档
                continue
            
            level_value = price * size
            
            if remaining_value <= level_value:
                # 这一档能吃完
                qty = remaining_value / price
                total_cost += remaining_value
                total_qty += qty
                remaining_value = 0
                break
            else:
                # 吃完这一档，继续下一档
                total_cost += level_value
                total_qty += size
                remaining_value -= level_value
        
        if total_qty == 0:
            return 0, 0
        
        # 计算 VWAP
        vwap = total_cost / total_qty
        
        # 计算滑点
        # 买单: 成交价高于当前价是正滑点
        # 卖单: 成交价低于当前价是正滑点
        if is_buy:
            slippage = (vwap - current_price) / current_price * 100
        else:
            slippage = (current_price - vwap) / current_price * 100
        
        return slippage, vwap
    
    def update_orderbook(self, cache_key: str, bids: List, asks: List):
        """
        更新订单簿缓存 (全量快照模式)
        
        Args:
            cache_key: 缓存键
            bids: 全量买单快照 [(price_str, size_str), ...]
            asks: 全量卖单快照
        """
        # 转换为 float 并排序
        # Bids: 价格降序
        parsed_bids = []
        for p_str, s_str in bids:
            parsed_bids.append((float(p_str), float(s_str)))
        parsed_bids.sort(key=lambda x: x[0], reverse=True)
            
        # Asks: 价格升序
        parsed_asks = []
        for p_str, s_str in asks:
            parsed_asks.append((float(p_str), float(s_str)))
        parsed_asks.sort(key=lambda x: x[0])
            
        # 直接替换旧数据
        self.orderbook_bids[cache_key] = parsed_bids
        self.orderbook_asks[cache_key] = parsed_asks
    
    def is_in_cooldown(self, key: str) -> bool:
        if key in self.alert_cooldown:
            if datetime.now() - self.alert_cooldown[key] < timedelta(seconds=self.cooldown_seconds):
                return True
        return False
    
    def set_cooldown(self, key: str):
        self.alert_cooldown[key] = datetime.now()
    
    async def connect_batch(self, symbols: List[str], batch_id: int) -> Optional[aiohttp.ClientWebSocketResponse]:
        """建立单批次连接 (使用代理轮换)"""
        retry_count = 0
        max_retries = 3
        
        while retry_count < max_retries:
            try:
                streams = []
                for s in symbols:
                    streams.append(f"{s}@aggTrade")
                    # 使用 @depth20@100ms 获取全量快照
                    streams.append(f"{s}@depth20@100ms")
                
                stream_param = "/".join(streams)
                url = f"{WS_COMBINED_URL}?streams={stream_param}"
                
                # 使用代理轮换器创建会话
                from connectors.proxy_rotator import create_session_with_proxy
                session, proxy_ip = await create_session_with_proxy()
                
                logger.info(f"🔄 连接 #{batch_id} 正在尝试 (代理: {proxy_ip})...")
                
                # 连接 WebSocket
                ws = await session.ws_connect(url, heartbeat=30, timeout=10)
                
                self._sessions.append(session)
                self._websockets.append(ws)
                self.stats['connections'] += 1
                
                logger.info(f"✅ 连接 #{batch_id} 成功 | 代理: {proxy_ip} | {len(symbols)} 交易对")
                return ws
                
            except Exception as e:
                retry_count += 1
                logger.warning(f"❌ 连接 #{batch_id} 失败 (尝试 {retry_count}/{max_retries}): {e} | 代理: {proxy_ip}")
                
                if 'session' in locals() and session and not session.closed:
                    await session.close()
                
                # 如果是代理问题，稍作等待后继续尝试下一个代理
                await asyncio.sleep(1)
        
        logger.error(f"🚫 连接 #{batch_id} 彻底失败，已重试 {max_retries} 次")
        return None
    
    async def disconnect_all(self):
        """断开所有连接"""
        self._running = False
        for ws in self._websockets:
            await ws.close()
        for session in self._sessions:
            await session.close()
        self._websockets.clear()
        self._sessions.clear()
    
    async def process_trade(self, symbol: str, price: float, size: float, is_buyer_maker: bool, market: str = "spot"):
        """处理成交 (VWAP 滑点检测 + 分级告警)"""
        self.stats['trades'] += 1
        
        value = price * size
        is_buy = not is_buyer_maker  # is_buyer_maker=True 表示卖方主动，即卖单
        side = "BUY" if is_buy else "SELL"
        
        # 更新价格缓存
        cache_key = f"{market}:{symbol}"
        self.price_cache[cache_key] = price
        
        # 检查最低金额
        min_order = self.get_min_order(market)
        if value < min_order:
            return
        
        # 计算滑点 (买单吃 Ask，卖单吃 Bid)
        slippage, vwap = self.calculate_slippage(cache_key, value, is_buy)
        
        # 获取告警级别
        level = self.get_alert_level(slippage)
        if not level:
            return
        
        key = f"{market}:{symbol}:trade:{int(price)}"
        if self.is_in_cooldown(key):
            return
        
        # 更新分级统计
        if level == AlertLevel.HIGH:
            self.stats['alerts_high'] += 1
        elif level == AlertLevel.MEDIUM:
            self.stats['alerts_medium'] += 1
        else:
            self.stats['alerts_low'] += 1
        
        await self.send_trade_alert(symbol, side, price, size, value, slippage, market, level)
        self.set_cooldown(key)
    
    async def process_depth(self, symbol: str, bids: List, asks: List, market: str = "spot"):
        """处理订单簿增量"""
        self.stats['depth_updates'] += 1
        
        cache_key = f"{market}:{symbol}"
        
        # 更新订单簿
        self.update_orderbook(cache_key, bids, asks)
        
        current_price = self.price_cache.get(cache_key, 0)
        if current_price <= 0:
            return
        
        min_order = self.get_min_order(market)
        
        # 检测买单大单 (检测新增的买墙)
        for price_str, size_str in bids:
            price = float(price_str)
            new_size = float(size_str)
            if new_size > 0:
                value = new_size * price
                if value >= min_order:
                    slippage, _ = self.calculate_slippage(cache_key, value, is_buy=False)
                    level = self.get_alert_level(slippage)
                    if level:
                        key = f"{market}:{symbol}:bid:{int(price)}"
                        if not self.is_in_cooldown(key):
                            if level == AlertLevel.HIGH:
                                self.stats['alerts_high'] += 1
                            elif level == AlertLevel.MEDIUM:
                                self.stats['alerts_medium'] += 1
                            else:
                                self.stats['alerts_low'] += 1
                            await self.send_order_alert(symbol, "BID", price, new_size, value, current_price, slippage, market, level)
                            self.set_cooldown(key)
        
        # 检测卖单大单 (检测新增的卖墙)
        for price_str, size_str in asks:
            price = float(price_str)
            new_size = float(size_str)
            if new_size > 0:
                value = new_size * price
                if value >= min_order:
                    slippage, _ = self.calculate_slippage(cache_key, value, is_buy=True)
                    level = self.get_alert_level(slippage)
                    if level:
                        key = f"{market}:{symbol}:ask:{int(price)}"
                        if not self.is_in_cooldown(key):
                            if level == AlertLevel.HIGH:
                                self.stats['alerts_high'] += 1
                            elif level == AlertLevel.MEDIUM:
                                self.stats['alerts_medium'] += 1
                            else:
                                self.stats['alerts_low'] += 1
                            await self.send_order_alert(symbol, "ASK", price, new_size, value, current_price, slippage, market, level)
                            self.set_cooldown(key)
    
    async def send_trade_alert(self, symbol: str, side: str, price: float, size: float, value: float, slippage: float, market: str = "spot", level: str = AlertLevel.MEDIUM):
        unified = SymbolConverter.from_binance(symbol.upper())
        side_icon = "🟢" if side == "BUY" else "🔴"
        market_tag = "📈合约" if market == "futures" else "💰现货"
        
        # 根据级别选择图标
        level_icon = {AlertLevel.LOW: "📊", AlertLevel.MEDIUM: "🐋", AlertLevel.HIGH: "🚨"}.get(level, "🐋")
        level_text = {AlertLevel.LOW: "", AlertLevel.MEDIUM: "", AlertLevel.HIGH: " ⚠️ 极端行情"}.get(level, "")
        
        message = f"""<b>{level_icon} 大额成交 {market_tag}{level_text}</b>
{unified} | {side_icon} {side}
💰 ${value:,.0f} | 滑点 {slippage:.2f}%
📍 @ ${price:,.2f}
⏰ {datetime.now().strftime('%H:%M:%S')}"""
        
        log_level = "warning" if level in (AlertLevel.MEDIUM, AlertLevel.HIGH) else "info"
        getattr(logger, log_level)(f"{level_icon} {market_tag} | {unified} | {side_icon} ${value:,.0f} @ ${price:,.2f} | 滑点 {slippage:.2f}%")
        await self.notifier.send(message, level=level)
    
    async def send_order_alert(self, symbol: str, side: str, price: float, size: float, value: float, current_price: float, slippage: float, market: str = "spot", level: str = AlertLevel.MEDIUM):
        unified = SymbolConverter.from_binance(symbol.upper())
        market_tag = "📈合约" if market == "futures" else "💰现货"
        
        if side == "BID":
            distance = (current_price - price) / current_price * 100 if current_price else 0
            icon = "🟩"
            side_text = "买墙"
        else:
            distance = (price - current_price) / current_price * 100 if current_price else 0
            icon = "🟥"
            side_text = "卖墙"
        
        # 根据级别添加标签
        level_prefix = {AlertLevel.LOW: "📊", AlertLevel.MEDIUM: "", AlertLevel.HIGH: "🚨"}.get(level, "")
        level_text = " ⚠️ 极端行情" if level == AlertLevel.HIGH else ""
        
        message = f"""<b>{level_prefix}{icon} 突发{side_text} {market_tag}{level_text}</b>
{unified} | ${value:,.0f} | 冲击 {slippage:.2f}%
📍 ${price:,.2f} (距现价 {distance:+.2f}%)
⏰ {datetime.now().strftime('%H:%M:%S')}"""
        
        log_level = "warning" if level in (AlertLevel.MEDIUM, AlertLevel.HIGH) else "info"
        getattr(logger, log_level)(f"{icon} {market_tag} {side_text} | {unified} | ${value:,.0f} @ ${price:,.2f} (现价 ${current_price:,.2f}) | 冲击 {slippage:.2f}%")
        await self.notifier.send(message, level=level)
    
    async def handle_connection(self, symbols: List[str], batch_id: int, market: str = "spot"):
        """
        处理单个批次的连接 (带自动重连)
        
        Args:
            symbols: 交易对列表
            batch_id: 批次 ID
            market: "spot" 或 "futures"
        """
        reconnect_delay = 1.0
        max_reconnect_delay = 60.0
        
        # 选择 WebSocket URL
        if market == "futures":
            ws_url = "wss://fstream.binance.com/stream"
            market_label = "合约"
        else:
            ws_url = WS_COMBINED_URL
            market_label = "现货"
        
        while self._running:
            ws = None
            session = None
            
            try:
                # 建立连接 (带重试和代理)
                streams = []
                for s in symbols:
                    streams.append(f"{s}@aggTrade")
                    # 使用 @depth20 获取全量快照 (1s 推送一次，减轻带宽和代理压力)
                    streams.append(f"{s}@depth20")
                stream_param = "/".join(streams)
                url = f"{ws_url}?streams={stream_param}"

                # 尝试连接重试循环
                retry_count = 0
                max_retries = 3
                ws = None
                session = None
                proxy_ip = "Unknown"

                while retry_count < max_retries and self._running:
                    try:
                        from connectors.proxy_rotator import create_session_with_proxy
                        session, proxy_ip = await create_session_with_proxy()
                        logger.info(f"🔄 {market_label}连接 #{batch_id} 尝试 (代理: {proxy_ip})...")
                        
                        # 增加超时以适应高延迟代理
                        ws = await session.ws_connect(url, heartbeat=60, timeout=30)
                        
                        self._sessions.append(session)
                        self._websockets.append(ws)
                        logger.info(f"✅ {market_label}连接 #{batch_id} 成功 | 代理: {proxy_ip} | {len(symbols)} 交易对")
                        break # 连接成功，跳出重试循环
                    except Exception as e:
                        retry_count += 1
                        logger.warning(f"❌ {market_label}连接 #{batch_id} 失败 (尝试 {retry_count}/{max_retries}): {e} | 代理: {proxy_ip}")
                        if session and not session.closed:
                            await session.close()
                        await asyncio.sleep(1)
                
                if not ws:
                    raise Exception(f"无法建立连接 (已重试 {max_retries} 次)")

                reconnect_delay = 1.0
                
                # 消息循环
                while self._running:
                    try:
                        msg = await ws.receive()
                        
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            
                            if "data" in data:
                                event_data = data["data"]
                                event_type = event_data.get("e")
                                
                                if event_type == "aggTrade":
                                    await self.process_trade(
                                        symbol=event_data["s"].lower(),
                                        price=float(event_data["p"]),
                                        size=float(event_data["q"]),
                                        is_buyer_maker=event_data["m"],
                                        market=market
                                    )
                                elif event_type == "depthUpdate":
                                    await self.process_depth(
                                        symbol=event_data["s"].lower(),
                                        bids=event_data.get("b", []),
                                        asks=event_data.get("a", []),
                                        market=market
                                    )
                        
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"⚠️ {market_label}连接 #{batch_id} 断开，准备重连...")
                            break
                            
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        logger.error(f"{market_label}连接 #{batch_id} 消息处理错误: {e}")
                        break
                
            except Exception as e:
                logger.error(f"{market_label}连接 #{batch_id} 建立失败: {e}")
            
            finally:
                # 清理当前连接 (防止列表无限增长)
                if ws:
                    await ws.close()
                    if ws in self._websockets:
                        self._websockets.remove(ws)
                if session:
                    await session.close()
                    if session in self._sessions:
                        self._sessions.remove(session)
            
            # 自动重连
            if self._running:
                logger.info(f"🔄 {market_label}连接 #{batch_id} 将在 {reconnect_delay:.0f}s 后重连...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
    
    async def run(self):
        """运行监控"""
        # 获取所有交易对 (现货 + 合约)
        spot_pairs, futures_pairs = await get_all_symbols()
        
        if not spot_pairs and not futures_pairs:
            logger.error("无法获取交易对")
            return
        
        spot_symbols = [p['symbol'] for p in spot_pairs]
        futures_symbols = [p['symbol'] for p in futures_pairs]
        
        # 获取代理数量
        from connectors.proxy_rotator import get_proxy_rotator
        proxy_rotator = get_proxy_rotator()
        proxy_count = max(proxy_rotator.count, 1)  # 至少算 1 个 (直连)
        
        # 币安限制: 每 IP 每 5 分钟 300 次连接请求
        # 保守配置: 每 IP 最多 50 个初始连接 (留余量给重连)
        MAX_CONNECTIONS_PER_IP = 50
        max_total_connections = proxy_count * MAX_CONNECTIONS_PER_IP
        
        # 计算理想连接数
        ideal_spot_connections = (len(spot_symbols) + MAX_SYMBOLS_PER_CONNECTION - 1) // MAX_SYMBOLS_PER_CONNECTION if self.monitor_spot else 0
        ideal_futures_connections = (len(futures_symbols) + MAX_SYMBOLS_PER_CONNECTION - 1) // MAX_SYMBOLS_PER_CONNECTION if self.monitor_futures else 0
        ideal_total = ideal_spot_connections + ideal_futures_connections
        
        # 如果超过限制，按比例缩减
        if ideal_total > max_total_connections:
            ratio = max_total_connections / ideal_total
            spot_connections = int(ideal_spot_connections * ratio)
            futures_connections = int(ideal_futures_connections * ratio)
            # 确保至少有 1 个连接
            spot_connections = max(1, spot_connections) if self.monitor_spot else 0
            futures_connections = max(1, futures_connections) if self.monitor_futures else 0
            
            # 重新计算每连接的 Symbol 数
            symbols_per_conn_spot = (len(spot_symbols) + spot_connections - 1) // spot_connections if spot_connections else 0
            symbols_per_conn_futures = (len(futures_symbols) + futures_connections - 1) // futures_connections if futures_connections else 0
            
            logger.warning(f"⚠️ 代理数量限制: {proxy_count} 个 IP × {MAX_CONNECTIONS_PER_IP} = 最多 {max_total_connections} 个连接")
            logger.warning(f"   理想连接数 {ideal_total} 超限，已缩减为 {spot_connections + futures_connections}")
            logger.warning(f"   现货每连接 {symbols_per_conn_spot} 交易对，合约每连接 {symbols_per_conn_futures} 交易对")
        else:
            spot_connections = ideal_spot_connections
            futures_connections = ideal_futures_connections
            symbols_per_conn_spot = MAX_SYMBOLS_PER_CONNECTION
            symbols_per_conn_futures = MAX_SYMBOLS_PER_CONNECTION
        
        total_connections = spot_connections + futures_connections
        
        print("\n" + "="*60)
        print("🚀 BINANCE 全量监控 (VWAP 滑点检测)")
        print("="*60)
        
        # 显示代理信息
        print(f"\n🌐 代理配置: {proxy_rotator.status()}")
        print(f"   每 IP 最大连接: {MAX_CONNECTIONS_PER_IP}")
        print(f"   允许总连接数: {max_total_connections}")
        
        # 显示配置
        print("\n📊 判定规则 (分级告警):")
        print(f"   LOW 阈值:    ≥ {self.slippage_low}%")
        print(f"   MEDIUM 阈值: ≥ {self.slippage_medium}%")
        print(f"   HIGH 阈值:   ≥ {self.slippage_high}%")
        print(f"   最低金额 (现货): ${self.min_order_spot:,.0f}")
        print(f"   最低金额 (合约): ${self.min_order_futures:,.0f}")
        print(f"   订单簿档位: {self.orderbook_depth} 档 (跳过前 {self.skip_top_levels} 档)")
        
        print()
        if self.monitor_spot:
            print(f"💰 现货: {len(spot_symbols)} 个交易对 ({spot_connections} 个连接)")
        else:
            print("💰 现货: 已关闭")
        if self.monitor_futures:
            print(f"📈 合约: {len(futures_symbols)} 个交易对 ({futures_connections} 个连接)")
        else:
            print("📈 合约: 已关闭")
        print(f"📊 总计: {total_connections} 个连接")
        print("="*60)
        
        # 显示部分交易对
        if self.monitor_spot and spot_symbols:
            print("\n📋 现货交易对 (前20):")
            spot_names = [SymbolConverter.from_binance(s.upper()) for s in spot_symbols[:20]]
            print("  " + ", ".join(spot_names))
        
        if self.monitor_futures and futures_symbols:
            print("\n📋 合约交易对 (前20):")
            futures_names = [SymbolConverter.from_binance(s.upper()) for s in futures_symbols[:20]]
            print("  " + ", ".join(futures_names))
        print()
        
        # 建立连接任务
        self._running = True
        tasks = []
        batch_id = 0
        
        # 现货连接
        if self.monitor_spot:
            for i in range(spot_connections):
                start_idx = i * symbols_per_conn_spot
                end_idx = min(start_idx + symbols_per_conn_spot, len(spot_symbols))
                batch_symbols = spot_symbols[start_idx:end_idx]
                batch_id += 1
                
                task = asyncio.create_task(self.handle_connection(batch_symbols, batch_id, "spot"))
                tasks.append(task)
                self.stats['connections'] += 1
                await asyncio.sleep(0.3)
        
        # 合约连接
        if self.monitor_futures:
            for i in range(futures_connections):
                start_idx = i * symbols_per_conn_futures
                end_idx = min(start_idx + symbols_per_conn_futures, len(futures_symbols))
                batch_symbols = futures_symbols[start_idx:end_idx]
                batch_id += 1
                
                task = asyncio.create_task(self.handle_connection(batch_symbols, batch_id, "futures"))
                tasks.append(task)
                self.stats['connections'] += 1
                await asyncio.sleep(0.3)
        
        if not tasks:
            logger.error("没有启动任何连接任务，请检查配置")
            return
        
        logger.info(f"✅ 启动 {len(tasks)} 个连接任务 (现货 {spot_connections} + 合约 {futures_connections})")
        
        # 状态显示
        async def show_stats():
            while self._running:
                await asyncio.sleep(30)
                runtime = datetime.now() - self.start_time
                rate = self.stats['trades'] / max(runtime.total_seconds(), 1)
                logger.info(
                    f"📊 {runtime} | "
                    f"连接 {self.stats['connections']} | "
                    f"成交 {self.stats['trades']:,} ({rate:.0f}/s) | "
                    f"告警 L:{self.stats['alerts_low']} M:{self.stats['alerts_medium']} H:{self.stats['alerts_high']}"
                )
        
        stats_task = asyncio.create_task(show_stats())
        tasks.append(stats_task)
        
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            pass
        finally:
            stats_task.cancel()
            await self.disconnect_all()
        
        # 统计
        runtime = datetime.now() - self.start_time
        print(f"\n📊 运行时长: {runtime}")
        print(f"📊 告警 LOW: {self.stats['alerts_low']}")
        print(f"📊 告警 MEDIUM: {self.stats['alerts_medium']}")
        print(f"📊 告警 HIGH: {self.stats['alerts_high']}")


async def main():
    monitor = BinanceMultiConnectionMonitor()
    await monitor.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 监控已停止")
