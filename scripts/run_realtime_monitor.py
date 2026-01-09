#!/usr/bin/env python3
"""
实时市场监控 (毫秒级)

同时监控：
1. 大单警报 (>= $1M)
2. 价格异常拉升/暴跌

使用:
    python run_realtime_monitor.py
"""
import asyncio
import logging
import threading
import queue
import sys
import os
from datetime import datetime
from typing import Optional

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lighter

from config import settings
from monitoring.large_order_monitor import LargeOrder
from monitoring.price_monitor import PriceMonitor, PriceAlert
from monitoring.telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s.%(msecs)03d | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class RealtimeMarketMonitor:
    """
    实时市场监控器
    
    同时检测大单和价格异常。
    """
    
    def __init__(
        self,
        # 大单配置
        min_value_usdc: float = 1000000.0,
        order_cooldown_sec: float = 60.0,
        # 价格异常配置
        pump_threshold_pct: float = 0.5,
        dump_threshold_pct: float = -0.5,
        price_time_window_sec: float = 60.0,
        price_cooldown_sec: float = 300.0,
        # Telegram
        telegram_token: str = "",
        telegram_chat_id: str = "",
    ):
        self.min_value_usdc = min_value_usdc
        self.order_cooldown_sec = order_cooldown_sec
        
        # Telegram
        self._telegram_token = telegram_token
        self._telegram_chat_id = telegram_chat_id
        self._has_telegram = bool(telegram_token and telegram_chat_id)
        
        # 消息队列 (线程安全)
        self._msg_queue: queue.Queue = queue.Queue()
        
        # 大单监控状态
        self._alerted: dict[float, datetime] = {}
        self._lock = threading.Lock()
        self._prev_bids: dict[str, str] = {}
        self._prev_asks: dict[str, str] = {}
        self._warmed_up = False  # 预热标志，首次加载后设为 True
        
        # 价格监控
        self._price_monitor = PriceMonitor(
            pump_threshold_pct=pump_threshold_pct,
            dump_threshold_pct=dump_threshold_pct,
            time_window_sec=price_time_window_sec,
            cooldown_sec=price_cooldown_sec,
        )
        
        # 统计
        self._total_order_alerts = 0
        self._total_price_alerts = 0
        self._running = False
    
    async def start(self):
        """启动监控"""
        self._running = True
        
        logger.info(f"🚀 实时市场监控启动")
        logger.info(f"   大单阈值: >= ${self.min_value_usdc:,.0f}")
        logger.info(f"   价格拉升: >= {self._price_monitor.pump_threshold_pct}%")
        logger.info(f"   价格暴跌: <= {self._price_monitor.dump_threshold_pct}%")
        logger.info(f"   Telegram: {'✅' if self._has_telegram else '❌'}")
        
        # 启动 WebSocket 线程
        self._ws_thread = threading.Thread(target=self._run_ws, daemon=True)
        self._ws_thread.start()
        
        # 启动消息发送循环
        asyncio.create_task(self._send_messages_loop())
    
    async def _send_messages_loop(self):
        """异步发送 Telegram 消息"""
        if not self._has_telegram:
            return
        
        notifier = TelegramNotifier(
            bot_token=self._telegram_token,
            chat_id=self._telegram_chat_id,
        )
        
        # 发送启动消息
        await notifier.send("🟢 <b>实时市场监控已启动</b>\n\n• 大单警报\n• 价格异常警报")
        
        while self._running:
            try:
                try:
                    msg_type, data = self._msg_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue
                
                if msg_type == "order" and data:
                    order: LargeOrder = data
                    await notifier.send_large_order_alert(
                        side=order.side,
                        price=order.price,
                        size=order.size,
                        value_usdc=order.value_usdc,
                    )
                elif msg_type == "price" and data:
                    alert: PriceAlert = data
                    await notifier.send_price_alert(
                        alert_type=alert.alert_type,
                        price_from=alert.price_from,
                        price_to=alert.price_to,
                        change_pct=alert.change_pct,
                        time_window_sec=alert.time_window_sec,
                    )
                elif msg_type == "停止":
                    await notifier.send("🔴 <b>实时市场监控已停止</b>")
                    
            except Exception as e:
                logger.error(f"发送消息错误: {e}")
        
        await notifier.close()
    
    def _run_ws(self):
        """运行 WebSocket"""
        try:
            client = lighter.WsClient(
                host="mainnet.zklighter.elliot.ai",
                order_book_ids=[0],  # ETH-USDC
                account_ids=[],
                on_order_book_update=self._on_orderbook_update,
            )
            client.run()
        except Exception as e:
            logger.error(f"WebSocket 错误: {e}")
            self._running = False
    
    def _on_orderbook_update(self, market_id, data: dict):
        """订单簿更新回调"""
        if not self._running:
            return
        
        now = datetime.now()
        
        bids = data.get('bids', [])
        asks = data.get('asks', [])
        
        current_bids = {b['price']: b['size'] for b in bids if isinstance(b, dict)}
        current_asks = {a['price']: a['size'] for a in asks if isinstance(a, dict)}
        
        # === 1. 大单检测 ===
        new_large_orders = []
        
        for price, size in current_bids.items():
            size_f = float(size)
            price_f = float(price)
            value = price_f * size_f
            
            if value < self.min_value_usdc:
                continue
            
            prev_size = float(self._prev_bids.get(price, '0'))
            if size_f > prev_size * 1.5 or price not in self._prev_bids:
                # 预热期跳过首次加载的警报
                if not self._warmed_up:
                    continue
                if not self._is_in_cooldown(price_f, now):
                    new_large_orders.append(LargeOrder(
                        side="bid",
                        price=price_f,
                        size=size_f,
                        value_usdc=value,
                        timestamp=now,
                    ))
        
        for price, size in current_asks.items():
            size_f = float(size)
            price_f = float(price)
            value = price_f * size_f
            
            if value < self.min_value_usdc:
                continue
            
            prev_size = float(self._prev_asks.get(price, '0'))
            if size_f > prev_size * 1.5 or price not in self._prev_asks:
                if not self._warmed_up:
                    continue
                if not self._is_in_cooldown(price_f, now):
                    new_large_orders.append(LargeOrder(
                        side="ask",
                        price=price_f,
                        size=size_f,
                        value_usdc=value,
                        timestamp=now,
                    ))
        
        for order in new_large_orders:
            self._trigger_order_alert(order)
        
        # === 2. 价格异常检测 ===
        # 使用最佳买卖价中间价
        if current_bids and current_asks:
            best_bid = max(current_bids.keys(), key=float)
            best_ask = min(current_asks.keys(), key=float)
            mid_price = (float(best_bid) + float(best_ask)) / 2
            
            price_alert = self._price_monitor.update(mid_price)
            if price_alert:
                self._trigger_price_alert(price_alert)
        
        # 更新状态
        self._prev_bids = current_bids
        self._prev_asks = current_asks
        
        # 首次加载完成后标记预热完成
        if not self._warmed_up:
            self._warmed_up = True
            logger.info("📊 预热完成，开始监控新变化")
    
    def _is_in_cooldown(self, price: float, now: datetime) -> bool:
        with self._lock:
            if price in self._alerted:
                elapsed = (now - self._alerted[price]).total_seconds()
                if elapsed < self.order_cooldown_sec:
                    return True
            return False
    
    def _trigger_order_alert(self, order: LargeOrder):
        with self._lock:
            self._alerted[order.price] = order.timestamp
            self._total_order_alerts += 1
        
        emoji = "🟢" if order.side == "bid" else "🔴"
        logger.warning(f"{emoji} 大单! {order}")
        
        if self._has_telegram:
            self._msg_queue.put(("order", order))
    
    def _trigger_price_alert(self, alert: PriceAlert):
        self._total_price_alerts += 1
        logger.warning(f"{alert}")
        
        if self._has_telegram:
            self._msg_queue.put(("price", alert))
    
    def stop(self):
        """停止监控"""
        if self._has_telegram:
            self._msg_queue.put(("停止", None))
        self._running = False
        logger.info(f"⏹️ 监控已停止 | 大单: {self._total_order_alerts} | 价格: {self._total_price_alerts}")


async def main():
    """主函数"""
    monitor = RealtimeMarketMonitor(
        min_value_usdc=settings.LARGE_ORDER_MIN_VALUE,
        pump_threshold_pct=getattr(settings, 'PRICE_PUMP_THRESHOLD', 0.5),
        dump_threshold_pct=getattr(settings, 'PRICE_DUMP_THRESHOLD', -0.5),
        telegram_token=settings.TELEGRAM_BOT_TOKEN,
        telegram_chat_id=settings.TELEGRAM_CHAT_ID,
    )
    
    await monitor.start()
    
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
