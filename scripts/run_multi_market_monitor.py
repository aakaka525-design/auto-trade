#!/usr/bin/env python3
"""
多市场实时监控 (WebSocket 原生实现)

同时监控多个市场的：
1. 大单警报
2. 价格异常拉升/暴跌

使用:
    python scripts/run_multi_market_monitor.py
"""
import asyncio
import json
import logging
import sys
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from dataclasses import dataclass

import aiohttp

# 添加项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from monitoring.large_order_monitor import LargeOrder
from monitoring.price_monitor import PriceMonitor, PriceAlert
from monitoring.telegram_notifier import TelegramNotifier
from connectors.lighter.markets import get_markets_sync, DEFAULT_MARKETS

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# 动态获取市场配置 (启动时从 API 加载或使用缓存)
MARKETS = get_markets_sync() or DEFAULT_MARKETS

WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"


class MultiMarketMonitor:
    """
    多市场实时监控器
    
    使用原生 WebSocket 连接，同时监控多个市场。
    支持分级大单阈值（主流币 vs 其他币）。
    """
    
    def __init__(
        self,
        market_ids: List[int] = None,
        min_value_major: float = 1000000.0,  # 主流币阈值
        min_value_other: float = 100000.0,   # 其他币阈值
        major_market_ids: List[int] = None,  # 主流币 ID 列表
        cooldown_sec: float = 60.0,
        pump_threshold_pct: float = 0.5,
        dump_threshold_pct: float = -0.5,
        telegram_token: str = "",
        telegram_chat_id: str = "",
    ):
        self.market_ids = market_ids or [0, 1, 2]
        self.min_value_major = min_value_major
        self.min_value_other = min_value_other
        self.major_market_ids = set(major_market_ids or [0, 1, 2, 7, 8, 9, 25])
        self.cooldown_sec = cooldown_sec
        
        # Telegram
        self._telegram_token = telegram_token
        self._telegram_chat_id = telegram_chat_id
        self._has_telegram = bool(telegram_token and telegram_chat_id)
        self._notifier: Optional[TelegramNotifier] = None
        
        # 每个市场的状态
        self._prev_orderbooks: Dict[int, dict] = {}
        self._alerted: Dict[str, datetime] = {}  # "市场:价格" -> 时间
        self._warmed_up: Dict[int, bool] = {m: False for m in self.market_ids}
        
        # 每个市场的价格监控
        self._price_monitors: Dict[int, PriceMonitor] = {}
        for market_id in self.market_ids:
            ticker = MARKETS.get(market_id, {}).get("ticker", f"MARKET-{market_id}")
            self._price_monitors[market_id] = PriceMonitor(
                pump_threshold_pct=pump_threshold_pct,
                dump_threshold_pct=dump_threshold_pct,
                symbol=ticker,
            )
        
        # 统计
        self._total_order_alerts = 0
        self._total_price_alerts = 0
        self._running = False
        
        # 重连后的静默期 (防止初始订单簿被误报为新增大单)
        self._quiet_until: Optional[datetime] = None
        self._quiet_period_sec = 5.0  # 重连后静默 5 秒
    
    def get_min_value_for_market(self, market_id: int) -> float:
        """获取指定市场的大单阈值"""
        if market_id in self.major_market_ids:
            return self.min_value_major
        return self.min_value_other
    
    async def start(self):
        """启动监控"""
        self._running = True
        
        markets_str = ", ".join(
            MARKETS.get(m, {}).get("ticker", str(m)) for m in self.market_ids
        )
        
        logger.info(f"🚀 多市场监控启动")
        logger.info(f"   市场: {markets_str}")
        logger.info(f"   大单阈值: 主流币 >= ${self.min_value_major:,.0f} | 其他 >= ${self.min_value_other:,.0f}")
        logger.info(f"   Telegram: {'✅' if self._has_telegram else '❌'}")
        
        if self._has_telegram:
            self._notifier = TelegramNotifier(
                bot_token=self._telegram_token,
                chat_id=self._telegram_chat_id,
            )
            await self._notifier.send(
                f"🟢 <b>多市场监控已启动</b>\n\n"
                f"• 市场: {len(self.market_ids)} 个\n"
                f"• 主流币阈值: ${self.min_value_major:,.0f}\n"
                f"• 其他币阈值: ${self.min_value_other:,.0f}"
            )
        
        # 连接 WebSocket
        await self._run_ws()
    
    async def _run_ws(self):
        """运行 WebSocket 连接 (带客户端心跳保活)"""
        reconnect_count = 0
        max_reconnects = 10
        
        while self._running and reconnect_count < max_reconnects:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        WS_URL,
                        heartbeat=30,  # 每 30 秒发送 ping
                        receive_timeout=90,  # 90 秒无消息则超时
                        autoping=True,
                    ) as ws:
                        logger.info(f"WebSocket 已连接: {WS_URL}")
                        reconnect_count = 0
                        
                        # 重连时清空状态，并设置静默期
                        self._prev_orderbooks.clear()
                        self._warmed_up = {m: False for m in self.market_ids}
                        self._quiet_until = datetime.now() + timedelta(seconds=self._quiet_period_sec)
                        logger.info(f"🔇 静默期: {self._quiet_period_sec}s")
                        # 清空价格监控历史
                        for pm in self._price_monitors.values():
                            pm.reset()
                        
                        # 订阅所有市场的订单簿
                        for market_id in self.market_ids:
                            sub_msg = {
                                "type": "subscribe",
                                "channel": f"order_book/{market_id}"
                            }
                            await ws.send_json(sub_msg)
                            ticker = MARKETS.get(market_id, {}).get("ticker", str(market_id))
                            logger.info(f"   订阅: {ticker}")
                        
                        # 启动心跳任务 (额外保活)
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop(ws)
                        )
                        
                        try:
                            # 监听消息
                            async for msg in ws:
                                if not self._running:
                                    break
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    data = json.loads(msg.data)
                                    # 处理服务器 ping 消息
                                    if data.get("type") == "ping":
                                        await ws.send_json({"type": "pong"})
                                        logger.debug("💓 收到服务器 ping，已回复 pong")
                                    else:
                                        await self._handle_message(data)
                                elif msg.type == aiohttp.WSMsgType.PING:
                                    # 标准 WebSocket PING
                                    await ws.pong(msg.data)
                                    logger.debug("💓 收到 WS PING")
                                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                    logger.warning(f"WebSocket 关闭: {msg.type}, data={msg.data}, extra={msg.extra}")
                                    break
                        finally:
                            heartbeat_task.cancel()
                            try:
                                await heartbeat_task
                            except asyncio.CancelledError:
                                pass
                        
            except asyncio.TimeoutError:
                reconnect_count += 1
                if self._running:
                    wait_time = min(2 ** reconnect_count, 30)
                    logger.warning(f"WebSocket 超时，{wait_time}s 后重连 ({reconnect_count}/{max_reconnects})")
                    await asyncio.sleep(wait_time)
            except aiohttp.ClientError as e:
                reconnect_count += 1
                if self._running:
                    wait_time = min(2 ** reconnect_count, 30)
                    logger.warning(f"WebSocket 断开，{wait_time}s 后重连: {e}")
                    await asyncio.sleep(wait_time)
            except Exception as e:
                reconnect_count += 1
                if self._running:
                    wait_time = min(2 ** reconnect_count, 30)
                    logger.error(f"WebSocket 错误: {e}")
                    await asyncio.sleep(wait_time)
        
        if reconnect_count >= max_reconnects:
            logger.error("WebSocket 重连次数耗尽")
    
    async def _heartbeat_loop(self, ws):
        """发送心跳保持连接活跃"""
        while self._running:
            try:
                await asyncio.sleep(45)  # 每 45 秒发送一次
                if ws.closed:
                    break
                # 发送空订阅消息作为心跳
                await ws.ping()
                logger.debug("💓 心跳发送")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"心跳异常: {e}")
                break
    
    async def _handle_message(self, data: dict):
        """处理 WebSocket 消息"""
        msg_type = data.get("type", "")
        
        if msg_type == "update/order_book":
            channel = data.get("channel", "")
            # channel 格式: "order_book:0"
            try:
                market_id = int(channel.split(":")[1])
            except:
                return
            
            order_book = data.get("order_book", {})
            await self._process_orderbook(market_id, order_book)
    
    async def _process_orderbook(self, market_id: int, data: dict):
        """处理订单簿更新"""
        now = datetime.now()
        ticker = MARKETS.get(market_id, {}).get("ticker", f"MARKET-{market_id}")
        
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        
        # 增量更新
        update_bids = {b["price"]: b["size"] for b in bids if isinstance(b, dict)}
        update_asks = {a["price"]: a["size"] for a in asks if isinstance(a, dict)}
        
        # 获取之前的完整状态
        prev = self._prev_orderbooks.get(market_id, {"bids": {}, "asks": {}})
        prev_bids = prev["bids"]
        prev_asks = prev["asks"]
        
        # 累积订单簿状态 (合并增量更新)
        full_bids = dict(prev_bids)  # 复制之前的状态
        full_asks = dict(prev_asks)
        
        # 应用更新 (size=0 表示删除)
        for price, size in update_bids.items():
            if float(size) <= 0:
                full_bids.pop(price, None)
            else:
                full_bids[price] = size
        
        for price, size in update_asks.items():
            if float(size) <= 0:
                full_asks.pop(price, None)
            else:
                full_asks[price] = size
        
        # === 大单检测 (检测已知价位的单次增量) ===
        # 新出现的价位只记录基线，不报警
        # 只有已存在价位的增量才触发警报
        if self._warmed_up.get(market_id, False):
            new_large_orders = []
            min_value = self.get_min_value_for_market(market_id)
            
            # 检测买单
            for price, size in update_bids.items():
                size_f = float(size)
                price_f = float(price)
                
                # 跳过删除操作 (size <= 0)
                if size_f <= 0:
                    continue
                
                # 🔑 关键: 只检测已存在价位的增量
                # 新价位不报警，只建立基线
                if price not in prev_bids:
                    continue
                
                # 计算增量
                prev_size = float(prev_bids[price])
                delta_size = size_f - prev_size
                
                # 只有正增量才可能是新挂单
                if delta_size <= 0:
                    continue
                
                # 检测增量价值是否超过阈值
                delta_value = price_f * delta_size
                if delta_value < min_value:
                    continue
                
                alert_key = f"{market_id}:bid:{price}"
                if not self._is_in_cooldown(alert_key, now):
                    new_large_orders.append(LargeOrder(
                        side="bid",
                        price=price_f,
                        size=delta_size,  # 报告增量，而非总量
                        value_usdc=delta_value,
                        timestamp=now,
                    ))
                    self._alerted[alert_key] = now
            
            # 检测卖单
            for price, size in update_asks.items():
                size_f = float(size)
                price_f = float(price)
                
                # 跳过删除操作
                if size_f <= 0:
                    continue
                
                # 🔑 关键: 只检测已存在价位的增量
                if price not in prev_asks:
                    continue
                
                # 计算增量
                prev_size = float(prev_asks[price])
                delta_size = size_f - prev_size
                
                # 只有正增量才可能是新挂单
                if delta_size <= 0:
                    continue
                
                # 检测增量价值是否超过阈值
                delta_value = price_f * delta_size
                if delta_value < min_value:
                    continue
                
                alert_key = f"{market_id}:ask:{price}"
                if not self._is_in_cooldown(alert_key, now):
                    # 调试日志：显示所有币种的增量计算
                    logger.debug(
                        f"🔍 {ticker}: price={price_f}, "
                        f"total={size_f}, prev={prev_size}, Δ={delta_size:.2f}, "
                        f"Δvalue=${delta_value:,.0f}"
                    )
                    new_large_orders.append(LargeOrder(
                        side="ask",
                        price=price_f,
                        size=delta_size,  # 报告增量，而非总量
                        value_usdc=delta_value,
                        timestamp=now,
                    ))
                    self._alerted[alert_key] = now
            
            # 发送警报
            for order in new_large_orders:
                await self._send_order_alert(market_id, order)
        
        # === 价格异常检测 (使用完整订单簿状态) ===
        if full_bids and full_asks:
            best_bid = max(full_bids.keys(), key=float)
            best_ask = min(full_asks.keys(), key=float)
            mid_price = (float(best_bid) + float(best_ask)) / 2
            
            price_monitor = self._price_monitors.get(market_id)
            if price_monitor:
                alert = price_monitor.update(mid_price)
                if alert:
                    await self._send_price_alert(market_id, alert)
        
        # 更新状态 (保存完整订单簿)
        self._prev_orderbooks[market_id] = {"bids": full_bids, "asks": full_asks}
        
        # 预热完成
        if not self._warmed_up.get(market_id, False):
            self._warmed_up[market_id] = True
            logger.info(f"📊 {ticker} 预热完成")
    
    def _is_in_cooldown(self, key: str, now: datetime) -> bool:
        if key in self._alerted:
            elapsed = (now - self._alerted[key]).total_seconds()
            return elapsed < self.cooldown_sec
        return False
    
    async def _send_order_alert(self, market_id: int, order: LargeOrder):
        """发送大单警报"""
        self._total_order_alerts += 1
        ticker = MARKETS.get(market_id, {}).get("ticker", f"MARKET-{market_id}")
        
        emoji = "🟢" if order.side == "bid" else "🔴"
        logger.warning(f"{emoji} [{ticker}] 新增Δ! {order}")
        
        if self._notifier:
            await self._notifier.send_large_order_alert(
                side=order.side,
                price=order.price,
                size=order.size,
                value_usdc=order.value_usdc,
                symbol=ticker,
            )
    
    async def _send_price_alert(self, market_id: int, alert: PriceAlert):
        """发送价格警报"""
        self._total_price_alerts += 1
        ticker = MARKETS.get(market_id, {}).get("ticker", f"MARKET-{market_id}")
        
        logger.warning(f"[{ticker}] {alert}")
        
        if self._notifier:
            await self._notifier.send_price_alert(
                alert_type=alert.alert_type,
                price_from=alert.price_from,
                price_to=alert.price_to,
                change_pct=alert.change_pct,
                time_window_sec=alert.time_window_sec,
                symbol=ticker,
            )
    
    async def stop(self):
        """停止监控"""
        self._running = False
        
        if self._notifier:
            await self._notifier.send("🔴 <b>多市场监控已停止</b>")
            await self._notifier.close()
        
        logger.info(f"⏹️ 监控已停止 | 大单: {self._total_order_alerts} | 价格: {self._total_price_alerts}")


async def main():
    """主函数"""
    # 主流加密货币市场 (避免订阅过多导致断连)
    # 可通过环境变量 MONITOR_MARKETS 自定义，例如: "0,1,2,3,7"
    import os
    
    # 默认 13 个主流币
    # 默认 13 个主流币
    default_markets = [0, 1, 2, 3, 7, 8, 9, 10, 12, 15, 16, 24, 25]
    
    # 从配置获取监控市场
    custom_markets = getattr(settings, 'MONITOR_MARKETS', '') or os.environ.get("MONITOR_MARKETS", "")
    if custom_markets.lower() == "all":
        market_ids = list(MARKETS.keys())
        logger.info(f"监控所有市场: {len(market_ids)} 个")
    elif custom_markets.lower() == "perp":
        market_ids = [mid for mid, m in MARKETS.items() if m.get("category") == "perp"]
        logger.info(f"监控永续合约: {len(market_ids)} 个")
    elif custom_markets:
        try:
            market_ids = [int(x.strip()) for x in custom_markets.split(",")]
            logger.info(f"使用自定义市场: {market_ids}")
        except:
            market_ids = default_markets
    else:
        market_ids = default_markets
    
    # 解析主流币ID
    major_ids_str = getattr(settings, 'MAJOR_MARKET_IDS', '0,1,2,7,8,9,25')
    major_market_ids = [int(x.strip()) for x in major_ids_str.split(",")]
    
    monitor = MultiMarketMonitor(
        market_ids=market_ids,
        min_value_major=getattr(settings, 'LARGE_ORDER_MIN_VALUE_MAJOR', 1000000.0),
        min_value_other=getattr(settings, 'LARGE_ORDER_MIN_VALUE_OTHER', 100000.0),
        major_market_ids=major_market_ids,
        pump_threshold_pct=getattr(settings, 'PRICE_PUMP_THRESHOLD', 0.5),
        dump_threshold_pct=getattr(settings, 'PRICE_DUMP_THRESHOLD', -0.5),
        telegram_token=settings.TELEGRAM_BOT_TOKEN,
        telegram_chat_id=settings.TELEGRAM_CHAT_ID,
    )
    
    try:
        await monitor.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"监控异常: {e}")
    finally:
        await monitor.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
