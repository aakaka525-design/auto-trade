"""
Binance 全量监控入口脚本

使用方法:
    python scripts/run_binance_monitor.py

环境变量:
    BINANCE_MONITOR_SPOT=true      # 监控现货
    BINANCE_MONITOR_FUTURES=true   # 监控合约
    SLIPPAGE_THRESHOLD_LOW=0.5     # 低级告警阈值
    SLIPPAGE_THRESHOLD_MED=2.0     # 中级告警阈值
    SLIPPAGE_THRESHOLD_HIGH=10.0   # 高级告警阈值
"""
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings

# 配置日志
log_dir = Path(__file__).parent.parent / "logs"
log_dir.mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-5s | %(message)s', datefmt='%H:%M:%S'))

file_handler = TimedRotatingFileHandler(
    log_dir / "binance_monitor.log",
    when="midnight",
    interval=1,
    backupCount=7,
    encoding="utf-8"
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

logger.addHandler(console_handler)
logger.addHandler(file_handler)


# ==================== 常量 ====================

# 每连接交易对数
# 现货数据量小，可以多一些
MAX_SYMBOLS_PER_CONN_SPOT = 100

# 合约数据量大，需要少一些避免断开 (降到 20 减少服务器压力)
MAX_SYMBOLS_PER_CONN_FUTURES = 20


# ==================== Telegram 通知 ====================

class TelegramNotifier:
    """Telegram 分级通知"""
    
    def __init__(self, token: str = "", chat_id: str = "", 
                 urgent_token: str = "", urgent_chat_id: str = ""):
        self.token = token
        self.chat_id = chat_id
        self.urgent_token = urgent_token or token
        self.urgent_chat_id = urgent_chat_id or chat_id
    
    async def send(self, message: str, level: str = "medium") -> bool:
        if not self.token or not self.chat_id:
            return False
        
        try:
            import aiohttp
            
            token = self.urgent_token if level == "high" else self.token
            chat_id = self.urgent_chat_id if level == "high" else self.chat_id
            
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=10) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.error(f"Telegram 发送失败: {e}")
            return False


# ==================== 监控入口 ====================

class BinanceMonitor:
    """Binance 监控器 (简化入口)"""
    
    def __init__(self):
        self.monitor_spot = getattr(settings, 'BINANCE_MONITOR_SPOT', True)
        self.monitor_futures = getattr(settings, 'BINANCE_MONITOR_FUTURES', True)
        self.start_time = datetime.now()
        self._running = False
        
        # 初始化组件
        self.notifier = TelegramNotifier(
            token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
            urgent_token=getattr(settings, 'TELEGRAM_URGENT_BOT_TOKEN', ''),
            urgent_chat_id=getattr(settings, 'TELEGRAM_URGENT_CHAT_ID', ''),
        )
        
        # 24h 成交量缓存 (symbol -> volume_usd)
        self.volume_cache: Dict[str, float] = {}
        
        # 动态深度阈值系数 (百分之一)
        self.depth_threshold_ratio = 0.01
        
        # 延迟加载
        self._processor = None
        self._ws_manager = None
    
    @property
    def processor(self):
        if self._processor is None:
            from monitoring.binance_processor import BinanceProcessor
            self._processor = BinanceProcessor(notifier=self.notifier)
        return self._processor
    
    @property
    def ws_manager(self):
        if self._ws_manager is None:
            from connectors.binance.websocket import BinanceWebSocketManager
            self._ws_manager = BinanceWebSocketManager(on_message=self._on_message)
        return self._ws_manager
    
    async def _on_message(self, data: dict, market: str):
        """WebSocket 消息回调"""
        event_type = data.get("e")
        
        if event_type == "aggTrade":
            await self.processor.process_trade(
                symbol=data["s"].lower(),
                price=float(data["p"]),
                size=float(data["q"]),
                is_buyer_maker=data["m"],
                market=market
            )
        elif event_type == "depthUpdate":
            await self.processor.process_depth(
                symbol=data["s"].lower(),
                bids=data.get("b", []),
                asks=data.get("a", []),
                market=market
            )
    
    async def run(self):
        """运行监控"""
        from connectors.binance.symbols import get_all_symbols
        
        # 获取交易对
        spot_pairs, futures_pairs = await get_all_symbols()
        
        if not spot_pairs and not futures_pairs:
            logger.error("无法获取交易对")
            return
        
        # 填充 24h 成交量缓存 (用于动态深度阈值)
        for p in spot_pairs:
            symbol = p['symbol'].upper()
            volume = p.get('volume', 0)
            self.volume_cache[symbol] = max(self.volume_cache.get(symbol, 0), volume)
        
        for p in futures_pairs:
            symbol = p['symbol'].upper()
            volume = p.get('volume', 0)
            self.volume_cache[symbol] = max(self.volume_cache.get(symbol, 0), volume)
        
        logger.info(f"已缓存 {len(self.volume_cache)} 个币种的 24h 成交量")
        
        spot_symbols = [p['symbol'] for p in spot_pairs] if self.monitor_spot else []
        futures_symbols = [p['symbol'] for p in futures_pairs] if self.monitor_futures else []
        
        # 计算连接数 (现货和合约使用不同配置)
        spot_connections = (len(spot_symbols) + MAX_SYMBOLS_PER_CONN_SPOT - 1) // MAX_SYMBOLS_PER_CONN_SPOT if spot_symbols else 0
        futures_connections = (len(futures_symbols) + MAX_SYMBOLS_PER_CONN_FUTURES - 1) // MAX_SYMBOLS_PER_CONN_FUTURES if futures_symbols else 0
        
        print("\n" + "=" * 60)
        print("🚀 BINANCE 全量监控 (重构版)")
        print("=" * 60)
        print(f"\n💰 现货: {len(spot_symbols)} 交易对 ({spot_connections} 连接)")
        print(f"📈 合约: {len(futures_symbols)} 交易对 ({futures_connections} 连接)")
        print(f"\n📊 滑点阈值: LOW≥{self.processor.slippage_low}% | MED≥{self.processor.slippage_medium}% | HIGH≥{self.processor.slippage_high}%")
        print("=" * 60 + "\n")
        
        # 启动连接
        self._running = True
        self.ws_manager.start()
        
        tasks = []
        batch_id = 0
        
        # 现货连接
        for i in range(spot_connections):
            start = i * MAX_SYMBOLS_PER_CONN_SPOT
            end = min(start + MAX_SYMBOLS_PER_CONN_SPOT, len(spot_symbols))
            batch_id += 1
            
            task = asyncio.create_task(
                self.ws_manager.handle_connection(spot_symbols[start:end], batch_id, "spot")
            )
            tasks.append(task)
            await asyncio.sleep(0.5)  # 增加连接间隔
        
        # 合约连接
        for i in range(futures_connections):
            start = i * MAX_SYMBOLS_PER_CONN_FUTURES
            end = min(start + MAX_SYMBOLS_PER_CONN_FUTURES, len(futures_symbols))
            batch_id += 1
            
            task = asyncio.create_task(
                self.ws_manager.handle_connection(futures_symbols[start:end], batch_id, "futures")
            )
            tasks.append(task)
            await asyncio.sleep(0.5)  # 增加连接间隔
        
        # 状态显示
        async def show_stats():
            while self._running:
                await asyncio.sleep(30)
                runtime = datetime.now() - self.start_time
                stats = self.processor.stats
                
                # WBI 统计
                wbi_stats = self.processor.book_imbalance.get_stats()
                ws_stats = self.ws_manager.stats
                
                # 基础统计
                logger.info(
                    f"📊 运行 {str(runtime).split('.')[0]} | "
                    f"成交 {stats['trades']:,} | "
                    f"告警 L:{stats['alerts_low']} M:{stats['alerts_medium']} H:{stats['alerts_high']} | "
                    f"WBI 活跃:{wbi_stats['active_alerts']} 热身:{wbi_stats['warmup_symbols']} | "
                    f"重连:{ws_stats['reconnects']}"
                )
                
                # === 智能算法: 检测深度不平衡 (WBI v3.x) ===
                # 动态深度阈值 = 24h 成交量 × 千分之一
                DEFAULT_MIN_DEPTH = 100000  # 默认 $100K (无成交量数据时)
                
                try:
                    wbi_signals = self.processor.get_pending_wbi_signals()
                    for sig in wbi_signals:
                        total_depth = sig.buy_power + sig.sell_power
                        
                        # 提取纯 symbol (去掉 market: 前缀)
                        if ":" in sig.symbol:
                            market, symbol = sig.symbol.split(":", 1)
                        else:
                            symbol = sig.symbol
                            market = "spot"
                        
                        # 动态阈值: 基于 24h 成交量 (注意: volume_cache 用大写 key)
                        volume_24h = self.volume_cache.get(symbol.upper(), 0)
                        if volume_24h > 0:
                            min_depth = volume_24h * self.depth_threshold_ratio
                        else:
                            min_depth = DEFAULT_MIN_DEPTH
                        
                        # 过滤深度不足的
                        if total_depth < min_depth:
                            continue
                        
                        market_label = "📈合约" if market == "futures" else "💰现货"
                        
                        direction = "🟢 买压" if sig.delta > 0 or sig.score > 0 else "🔴 卖压"
                        
                        # 获取价格（从 processor 缓存）
                        cache_key = sig.symbol
                        price = self.processor.price_cache.get(cache_key, 0)
                        price_str = f" @ ${price:,.0f}" if price > 0 else ""
                        
                        msg = (
                            f"📊 *深度不平衡信号 (WBI v3.0)*\n"
                            f"市场: {market_label}\n"
                            f"币种: {symbol.upper()}{price_str}\n"
                            f"方向: {direction}\n"
                            f"触发: {sig.trigger_reason}\n"
                            f"挂单量: ${total_depth:,.0f}"
                        )
                        logger.warning(f"📊 WBI | {market_label} {symbol.upper()}{price_str} | {direction} | {sig.trigger_reason} | 挂单 ${total_depth/1000:.0f}K")
                        await self.notifier.send(msg, "medium")
                except Exception as e:
                    logger.debug(f"WBI 处理异常: {e}")
                
                # === 基差警报 ===
                try:
                    basis_alerts = self.processor.get_pending_basis_alerts()
                    for alert in basis_alerts:
                        direction = "📈 合约溢价" if alert.basis_pct > 0 else "📉 合约折价"
                        msg = (
                            f"💹 *现货/合约基差警报*\n"
                            f"币种: {alert.symbol}\n"
                            f"方向: {direction}\n"
                            f"基差: {alert.basis_pct:+.2f}%\n"
                            f"现货: ${alert.spot_price:,.2f}\n"
                            f"合约: ${alert.futures_price:,.2f}"
                        )
                        logger.warning(f"💹 基差 | {alert.symbol} | {direction} | {alert.basis_pct:+.2f}% | 现货 ${alert.spot_price:,.0f} 合约 ${alert.futures_price:,.0f}")
                        await self.notifier.send(msg, "high")
                except Exception as e:
                    logger.debug(f"基差处理异常: {e}")
                
                # === 智能算法: 检测鲸鱼模式 ===
                try:
                    whale_patterns = self.processor.whale_tracker.get_all_patterns()
                    for p in whale_patterns:
                        if p.confidence >= 0.8:
                            pattern_names = {
                                "accumulation": "🟢 大户建仓",
                                "distribution": "🔴 大户出货",
                                "price_wall": "🧱 价格墙",
                            }
                            pattern_name = pattern_names.get(p.pattern_type.value, p.pattern_type.value)
                            msg = (
                                f"🐋 *鲸鱼行为检测*\n"
                                f"币种: {p.symbol.upper()}\n"
                                f"模式: {pattern_name}\n"
                                f"金额: ${p.total_value:,.0f}\n"
                                f"置信度: {p.confidence:.0%}\n"
                                f"详情: {p.description}"
                            )
                            logger.warning(f"🐋 鲸鱼 | {p.symbol.upper()} | {pattern_name} | ${p.total_value:,.0f}")
                            await self.notifier.send(msg, "medium")
                except Exception:
                    pass
                
                # === 智能算法: 检测 Stop Hunt ===
                try:
                    trackers = list(self.processor.whale_tracker._trackers.keys())[:20]
                    for symbol in trackers:
                        stop_hunt = self.processor.whale_tracker.detect_stop_hunt(symbol)
                        if stop_hunt.is_detected:
                            msg = (
                                f"🎯 *Stop Hunt 猎杀检测*\n"
                                f"币种: {symbol.upper()}\n"
                                f"支撑位: ${stop_hunt.support_price:,.2f}\n"
                                f"击穿至: ${stop_hunt.breakthrough_price:,.2f}\n"
                                f"反弹至: ${stop_hunt.rebound_price:,.2f}\n"
                                f"成交量: {stop_hunt.volume_spike_ratio:.1f}x 平均"
                            )
                            logger.warning(f"🎯 Stop Hunt | {symbol.upper()} | 击穿后反弹")
                            await self.notifier.send(msg, "high")  # 高优先级推送
                except Exception:
                    pass
        
        stats_task = asyncio.create_task(show_stats())
        tasks.append(stats_task)
        
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            stats_task.cancel()
            await self.ws_manager.disconnect_all()
        
        # 统计
        runtime = datetime.now() - self.start_time
        stats = self.processor.stats
        print(f"\n📊 运行时长: {runtime}")
        print(f"📊 告警 LOW: {stats['alerts_low']} | MEDIUM: {stats['alerts_medium']} | HIGH: {stats['alerts_high']}")


async def main():
    monitor = BinanceMonitor()
    await monitor.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 监控已停止")
