#!/usr/bin/env python3
"""
统一监控入口

同时运行 Lighter DEX 和 Binance 监控，提供统一的控制和统计。

使用:
    python scripts/run_unified_monitor.py

环境变量:
    MONITOR_EXCHANGES: 监控的交易所列表，逗号分隔 (默认: "lighter,binance")
    
示例:
    # 只监控 Binance
    MONITOR_EXCHANGES=binance python scripts/run_unified_monitor.py
    
    # 同时监控两个
    MONITOR_EXCHANGES=lighter,binance python scripts/run_unified_monitor.py
"""
import asyncio
import logging
import sys
import signal
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from config import settings

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class UnifiedMonitor:
    """
    统一监控管理器
    
    特性:
    - 同时管理多个交易所监控
    - 统一的启动/停止控制
    - 聚合统计信息
    - 优雅关闭处理
    """
    
    def __init__(self, exchanges: list[str] = None):
        self.exchanges = exchanges or self._parse_exchanges()
        self._tasks: Dict[str, asyncio.Task] = {}
        self._running = False
        self._start_time: Optional[datetime] = None
        
        # 统计 (各交易所聚合)
        self.stats = {
            'lighter': {'trades': 0, 'alerts': 0},
            'binance': {'trades': 0, 'alerts': 0},
        }
    
    def _parse_exchanges(self) -> list[str]:
        """从配置解析交易所列表"""
        exchanges_str = getattr(settings, 'MONITOR_EXCHANGES', 'lighter,binance')
        return [e.strip().lower() for e in exchanges_str.split(',') if e.strip()]
    
    async def start(self):
        """启动所有监控"""
        self._running = True
        self._start_time = datetime.now()
        
        print("\n" + "=" * 60)
        print("🚀 统一监控入口")
        print("=" * 60)
        print(f"监控交易所: {', '.join(self.exchanges)}")
        print(f"启动时间: {self._start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60 + "\n")
        
        # 启动各交易所监控
        for exchange in self.exchanges:
            if exchange == 'lighter':
                self._tasks['lighter'] = asyncio.create_task(
                    self._run_lighter_monitor()
                )
            elif exchange == 'binance':
                self._tasks['binance'] = asyncio.create_task(
                    self._run_binance_monitor()
                )
            else:
                logger.warning(f"未知交易所: {exchange}")
        
        # 启动统计任务
        stats_task = asyncio.create_task(self._stats_loop())
        
        # 等待所有任务
        try:
            await asyncio.gather(*self._tasks.values(), stats_task, return_exceptions=True)
        except asyncio.CancelledError:
            pass
    
    async def stop(self):
        """停止所有监控"""
        print("\n👋 正在停止监控...")
        self._running = False
        
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self._print_final_stats()
    
    async def _run_lighter_monitor(self):
        """运行 Lighter 监控"""
        try:
            from scripts.run_multi_market_monitor import MultiMarketMonitor
            from monitoring.large_order_monitor import LargeOrder
            
            # 解析市场 ID
            market_ids_str = getattr(settings, 'MONITOR_MARKETS', '')
            if market_ids_str and market_ids_str != 'all':
                try:
                    market_ids = [int(m.strip()) for m in market_ids_str.split(',') if m.strip().isdigit()]
                except ValueError:
                    market_ids = None
            else:
                market_ids = None  # 使用默认
            
            # 解析主流币 ID
            major_ids_str = getattr(settings, 'MAJOR_MARKET_IDS', '0,1,2,7,8,9,25')
            major_ids = [int(m.strip()) for m in major_ids_str.split(',') if m.strip().isdigit()]
            
            logger.info("🔄 正在初始化 Lighter 监控...")
            
            monitor = MultiMarketMonitor(
                market_ids=market_ids,
                min_value_major=settings.LARGE_ORDER_MIN_VALUE_MAJOR,
                min_value_other=settings.LARGE_ORDER_MIN_VALUE_OTHER,
                major_market_ids=major_ids,
                cooldown_sec=settings.PRICE_COOLDOWN,
                pump_threshold_pct=settings.PRICE_PUMP_THRESHOLD,
                dump_threshold_pct=settings.PRICE_DUMP_THRESHOLD,
                telegram_token=settings.TELEGRAM_BOT_TOKEN,
                telegram_chat_id=settings.TELEGRAM_CHAT_ID,
            )
            
            logger.info("✅ Lighter 监控已启动")
            await monitor.start()
            
        except ImportError as e:
            logger.error(f"❌ Lighter 监控依赖缺失: {e}")
        except Exception as e:
            import traceback
            logger.error(f"❌ Lighter 监控异常: {e}")
            logger.error(traceback.format_exc())
    
    async def _run_binance_monitor(self):
        """运行 Binance 监控"""
        try:
            from scripts.run_binance_monitor import BinanceMultiConnectionMonitor
            
            monitor = BinanceMultiConnectionMonitor()
            
            logger.info("✅ Binance 监控已启动")
            await monitor.run()
            
        except ImportError as e:
            logger.error(f"Binance 监控依赖缺失: {e}")
        except Exception as e:
            logger.error(f"Binance 监控异常: {e}")
    
    async def _stats_loop(self):
        """统计输出循环"""
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break
            self._print_stats()
    
    def _print_stats(self):
        """打印统计信息"""
        runtime = datetime.now() - self._start_time if self._start_time else None
        tasks_running = sum(1 for t in self._tasks.values() if not t.done())
        
        logger.info(
            f"📊 统一监控 | 运行 {runtime} | "
            f"活跃任务 {tasks_running}/{len(self._tasks)}"
        )
    
    def _print_final_stats(self):
        """打印最终统计"""
        runtime = datetime.now() - self._start_time if self._start_time else None
        
        print("\n" + "=" * 60)
        print("📊 监控统计")
        print("=" * 60)
        print(f"运行时长: {runtime}")
        print(f"监控交易所: {', '.join(self.exchanges)}")
        print("=" * 60 + "\n")


async def main():
    """主函数"""
    monitor = UnifiedMonitor()
    
    # 信号处理
    loop = asyncio.get_event_loop()
    _stop_count = 0
    
    def signal_handler():
        nonlocal _stop_count
        _stop_count += 1
        
        if _stop_count == 1:
            print("\n👋 正在优雅停止... (再按 Ctrl+C 强制退出)")
            asyncio.create_task(monitor.stop())
        else:
            print("\n⚠️ 强制退出!")
            import sys
            sys.exit(1)
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows 不支持
            pass
    
    try:
        await monitor.start()
    except KeyboardInterrupt:
        await monitor.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 监控已停止")
    except SystemExit:
        pass

