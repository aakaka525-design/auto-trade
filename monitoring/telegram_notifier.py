"""
Telegram 警报通知

发送交易警报到 Telegram。
"""
import asyncio
import logging
from typing import Optional
from datetime import datetime

import aiohttp

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """
    Telegram 通知器
    
    使用示例:
    ```python
    notifier = TelegramNotifier(
        bot_token="YOUR_BOT_TOKEN",
        chat_id="YOUR_CHAT_ID"
    )
    
    await notifier.send("🚨 大单警报: $100,000 买单 @ $3100")
    ```
    
    获取 Bot Token:
    1. 在 Telegram 搜索 @BotFather
    2. 发送 /newbot 创建机器人
    3. 保存获得的 token
    
    获取 Chat ID:
    1. 搜索你的机器人并发送 /start
    2. 访问 https://api.telegram.org/bot<TOKEN>/getUpdates
    3. 从响应中找到 chat.id
    """
    
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        parse_mode: str = "HTML",
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.parse_mode = parse_mode
        
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._session: Optional[aiohttp.ClientSession] = None
        
        # 限流
        self._last_send_time: Optional[datetime] = None
        self._min_interval = 1.0  # 最小发送间隔(秒)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def send(self, message: str, disable_notification: bool = False) -> bool:
        """
        发送消息
        
        Args:
            message: 消息内容 (支持 HTML 格式)
            disable_notification: 是否静音
        
        Returns:
            是否发送成功
        """
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram 配置不完整，跳过发送")
            return False
        
        # 限流检查
        now = datetime.now()
        if self._last_send_time:
            elapsed = (now - self._last_send_time).total_seconds()
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
        
        try:
            session = await self._get_session()
            
            url = f"{self._base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": self.parse_mode,
                "disable_notification": disable_notification,
            }
            
            async with session.post(url, json=data, timeout=10) as resp:
                result = await resp.json()
                
                if result.get("ok"):
                    self._last_send_time = now
                    logger.debug(f"Telegram 发送成功: {message[:50]}...")
                    return True
                else:
                    logger.error(f"Telegram 发送失败: {result}")
                    return False
                    
        except asyncio.TimeoutError:
            logger.error("Telegram 发送超时")
            return False
        except Exception as e:
            logger.error(f"Telegram 发送错误: {e}")
            return False
    
    async def send_large_order_alert(
        self,
        side: str,
        price: float,
        size: float,
        value_usdc: float,
        symbol: str = "ETH-USDC",
    ) -> bool:
        """发送大单警报"""
        emoji = "🟢" if side == "bid" else "🔴"
        side_text = "买入" if side == "bid" else "卖出"
        
        message = (
            f"{emoji} <b>大单警报 - {symbol}</b>\n\n"
            f"方向: <b>{side_text}</b>\n"
            f"价格: <code>${price:,.2f}</code>\n"
            f"数量: <code>{size:.4f}</code>\n"
            f"价值: <code>${value_usdc:,.0f}</code>\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        
        return await self.send(message)
    
    async def send_summary(
        self,
        bid_count: int,
        bid_value: float,
        ask_count: int,
        ask_value: float,
        symbol: str = "ETH-USDC",
    ) -> bool:
        """发送大单汇总"""
        message = (
            f"📊 <b>大单汇总 - {symbol}</b>\n\n"
            f"🟢 买单: {bid_count} 个, 总价值 <code>${bid_value:,.0f}</code>\n"
            f"🔴 卖单: {ask_count} 个, 总价值 <code>${ask_value:,.0f}</code>\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        
        return await self.send(message)
    
    async def send_price_alert(
        self,
        alert_type: str,
        price_from: float,
        price_to: float,
        change_pct: float,
        time_window_sec: float,
        symbol: str = "ETH-USDC",
    ) -> bool:
        """发送价格异常警报"""
        if alert_type == "pump":
            emoji = "🚀"
            title = "价格拉升"
        else:
            emoji = "💥"
            title = "价格暴跌"
        
        message = (
            f"{emoji} <b>{title} - {symbol}</b>\n\n"
            f"涨跌幅: <b>{change_pct:+.2f}%</b>\n"
            f"价格: <code>${price_from:,.2f}</code> → <code>${price_to:,.2f}</code>\n"
            f"时间窗口: {time_window_sec:.0f}秒\n\n"
            f"⏰ {datetime.now().strftime('%H:%M:%S')}"
        )
        
        return await self.send(message)
    
    async def close(self):
        """关闭会话"""
        if self._session and not self._session.closed:
            await self._session.close()


# 全局实例
_notifier: Optional[TelegramNotifier] = None


def get_telegram_notifier() -> Optional[TelegramNotifier]:
    """获取全局 Telegram 通知器"""
    return _notifier


def init_telegram(bot_token: str, chat_id: str) -> TelegramNotifier:
    """初始化全局 Telegram 通知器"""
    global _notifier
    _notifier = TelegramNotifier(bot_token=bot_token, chat_id=chat_id)
    return _notifier
