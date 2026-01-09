#!/usr/bin/env python3
"""
大单监控脚本

实时监控 Lighter 订单簿，发现大单时发送 Telegram 警报。

使用:
    python run_large_order_monitor.py

配置 .env:
    TELEGRAM_BOT_TOKEN=your_bot_token
    TELEGRAM_CHAT_ID=your_chat_id
    LARGE_ORDER_MIN_SIZE=10.0
    LARGE_ORDER_MIN_VALUE=30000.0
"""
import asyncio
import logging
import time
import sys
import os
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from connectors import LighterConnector
from monitoring.large_order_monitor import LargeOrderMonitor, LargeOrder
from monitoring.telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger(__name__)


class LargeOrderAlertBot:
    """大单警报机器人"""
    
    def __init__(
        self,
        min_size: float = 10.0,
        min_value: float = 30000.0,
        check_interval: float = 5.0,  # 检查间隔(秒)
        summary_interval: float = 300.0,  # 汇总间隔(秒)
    ):
        self.min_size = min_size
        self.min_value = min_value
        self.check_interval = check_interval
        self.summary_interval = summary_interval
        
        # 组件
        self.connector: LighterConnector = None
        self.monitor: LargeOrderMonitor = None
        self.notifier: TelegramNotifier = None
        
        # 状态
        self._running = False
        self._last_summary_time = None
    
    async def initialize(self) -> bool:
        """初始化"""
        logger.info("🚀 初始化大单监控...")
        
        # 连接器
        self.connector = LighterConnector({
            'base_url': settings.LIGHTER_BASE_URL,
            'account_index': settings.LIGHTER_ACCOUNT_INDEX,
            'api_key_index': settings.LIGHTER_API_KEY_INDEX,
            'api_private_key': settings.LIGHTER_API_PRIVATE_KEY,
            'http_proxy': settings.HTTP_PROXY,
            'https_proxy': settings.HTTPS_PROXY,
        })
        
        if not await self.connector.connect():
            logger.error("连接失败")
            return False
        
        # 等待 WebSocket 数据
        logger.info("等待订单簿数据...")
        time.sleep(3)
        
        # Telegram
        bot_token = getattr(settings, 'TELEGRAM_BOT_TOKEN', '')
        chat_id = getattr(settings, 'TELEGRAM_CHAT_ID', '')
        
        if bot_token and chat_id:
            self.notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
            logger.info("✅ Telegram 已配置")
        else:
            logger.warning("⚠️ Telegram 未配置，仅本地输出")
        
        # 监控器
        self.monitor = LargeOrderMonitor(
            min_size=self.min_size,
            min_value_usdc=self.min_value,
            on_alert=self._on_alert,
        )
        
        logger.info(f"✅ 初始化完成 (min_size={self.min_size}, min_value=${self.min_value:,.0f})")
        return True
    
    def _on_alert(self, order: LargeOrder) -> None:
        """大单警报回调"""
        # 同步发送 Telegram (在后台)
        if self.notifier:
            asyncio.create_task(
                self.notifier.send_large_order_alert(
                    side=order.side,
                    price=order.price,
                    size=order.size,
                    value_usdc=order.value_usdc,
                )
            )
    
    async def run(self) -> None:
        """运行监控"""
        if not await self.initialize():
            return
        
        self._running = True
        self._last_summary_time = datetime.now()
        
        logger.info("📊 开始监控大单...")
        
        if self.notifier:
            await self.notifier.send("🟢 <b>大单监控已启动</b>")
        
        try:
            while self._running:
                await self._check_once()
                await asyncio.sleep(self.check_interval)
                
        except KeyboardInterrupt:
            logger.info("收到停止信号")
        finally:
            await self.stop()
    
    async def _check_once(self) -> None:
        """单次检查"""
        try:
            ob = await self.connector.get_orderbook("ETH-USDC", depth=0)
            
            # 检查大单
            large_orders = self.monitor.check(ob)
            
            # 定期汇总
            now = datetime.now()
            if self._last_summary_time:
                elapsed = (now - self._last_summary_time).total_seconds()
                if elapsed >= self.summary_interval:
                    await self._send_summary()
                    self._last_summary_time = now
                    
        except Exception as e:
            logger.error(f"检查错误: {e}")
    
    async def _send_summary(self) -> None:
        """发送汇总"""
        stats = self.monitor.get_stats()
        recent = self.monitor.get_recent_orders(20)
        
        bid_orders = [o for o in recent if o.side == 'bid']
        ask_orders = [o for o in recent if o.side == 'ask']
        
        bid_value = sum(o.value_usdc for o in bid_orders)
        ask_value = sum(o.value_usdc for o in ask_orders)
        
        logger.info(f"📊 汇总: {len(bid_orders)} 买单 ${bid_value:,.0f}, {len(ask_orders)} 卖单 ${ask_value:,.0f}")
        
        if self.notifier and (bid_orders or ask_orders):
            await self.notifier.send_summary(
                bid_count=len(bid_orders),
                bid_value=bid_value,
                ask_count=len(ask_orders),
                ask_value=ask_value,
            )
    
    async def stop(self) -> None:
        """停止"""
        self._running = False
        
        if self.notifier:
            await self.notifier.send("🔴 <b>大单监控已停止</b>")
            await self.notifier.close()
        
        if self.connector:
            await self.connector.disconnect()
        
        logger.info("⏹️ 监控已停止")


async def main():
    """主函数"""
    bot = LargeOrderAlertBot(
        min_size=getattr(settings, 'LARGE_ORDER_MIN_SIZE', 10.0),
        min_value=getattr(settings, 'LARGE_ORDER_MIN_VALUE', 30000.0),
        check_interval=5.0,
        summary_interval=300.0,
    )
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
