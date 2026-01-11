"""
Lighter 市场配置

从 API 动态获取市场列表，或使用缓存。
"""
import asyncio
import aiohttp
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# API 端点
ORDERBOOKS_API = "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks"

# 缓存路径
CACHE_FILE = Path(__file__).parent / "markets_cache.json"
CACHE_TTL_HOURS = 24  # 缓存有效期


async def fetch_markets_from_api() -> Dict[int, dict]:
    """
    从 API 获取所有市场配置
    
    Returns:
        {market_id: {"ticker": "ETH-USDC", "category": "perp", ...}}
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ORDERBOOKS_API, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"API 请求失败: {resp.status}")
                    return {}
                
                data = await resp.json()
                markets = {}
                
                for ob in data.get("order_books", []):
                    market_id = ob.get("market_id")
                    symbol = ob.get("symbol", "")
                    market_type = ob.get("market_type", "perp")
                    
                    if market_id is not None:
                        # 现货市场 symbol 已包含 /USDC，不需要再加后缀
                        if "/" in symbol:
                            ticker = symbol.replace("/", "-")  # ETH/USDC -> ETH-USDC
                        else:
                            ticker = f"{symbol}-USDC"
                        
                        markets[market_id] = {
                            "ticker": ticker,
                            "symbol": symbol,
                            "category": market_type,
                            "status": ob.get("status", "active"),
                            "price_decimals": ob.get("supported_price_decimals", 2),
                            "size_decimals": ob.get("supported_size_decimals", 4),
                        }
                
                logger.info(f"从 API 获取 {len(markets)} 个市场")
                return markets
                
    except Exception as e:
        logger.error(f"获取市场列表失败: {e}")
        return {}


def save_cache(markets: Dict[int, dict]) -> None:
    """保存市场缓存"""
    try:
        cache_data = {
            "updated_at": datetime.now().isoformat(),
            "markets": {str(k): v for k, v in markets.items()}
        }
        with open(CACHE_FILE, "w") as f:
            json.dump(cache_data, f, indent=2)
        logger.info(f"市场缓存已保存: {CACHE_FILE}")
    except Exception as e:
        logger.error(f"保存缓存失败: {e}")


def load_cache() -> Optional[Dict[int, dict]]:
    """加载市场缓存"""
    try:
        if not CACHE_FILE.exists():
            return None
        
        with open(CACHE_FILE, "r") as f:
            cache_data = json.load(f)
        
        # 检查缓存是否过期
        updated_at = datetime.fromisoformat(cache_data.get("updated_at", "2000-01-01"))
        if datetime.now() - updated_at > timedelta(hours=CACHE_TTL_HOURS):
            logger.info("市场缓存已过期")
            return None
        
        markets = {int(k): v for k, v in cache_data.get("markets", {}).items()}
        logger.info(f"从缓存加载 {len(markets)} 个市场")
        return markets
        
    except Exception as e:
        logger.error(f"加载缓存失败: {e}")
        return None


async def get_markets(force_refresh: bool = False) -> Dict[int, dict]:
    """
    获取市场配置 (优先使用缓存)
    
    Args:
        force_refresh: 强制刷新缓存
    
    Returns:
        市场配置字典
    """
    if not force_refresh:
        cached = load_cache()
        if cached:
            return cached
    
    markets = await fetch_markets_from_api()
    
    if markets:
        save_cache(markets)
    
    return markets


def get_markets_sync(force_refresh: bool = False) -> Dict[int, dict]:
    """
    同步版本的 get_markets
    
    兼容:
    1. 没有事件循环时: 使用 asyncio.run()
    2. 在运行的事件循环中: 优先使用缓存避免冲突
    """
    # 优先尝试使用缓存 (避免事件循环冲突)
    if not force_refresh:
        cached = load_cache()
        if cached:
            return cached
    
    # 检查是否在事件循环中
    try:
        loop = asyncio.get_running_loop()
        # 在事件循环中，返回默认值或缓存
        logger.warning("在事件循环中无法同步获取市场，使用缓存或默认值")
        return load_cache() or DEFAULT_MARKETS
    except RuntimeError:
        # 没有运行的事件循环，可以使用 asyncio.run()
        return asyncio.run(get_markets(force_refresh))


# 默认主流币 (备用)
DEFAULT_MARKETS = {
    0: {"ticker": "ETH-USDC", "symbol": "ETH", "category": "perp"},
    1: {"ticker": "BTC-USDC", "symbol": "BTC", "category": "perp"},
    2: {"ticker": "SOL-USDC", "symbol": "SOL", "category": "perp"},
}


if __name__ == "__main__":
    # 测试
    logging.basicConfig(level=logging.INFO)
    
    async def main():
        markets = await get_markets(force_refresh=True)
        print(f"\n共 {len(markets)} 个市场:\n")
        
        for mid in sorted(markets.keys())[:30]:
            m = markets[mid]
            print(f"  {mid}: {m['ticker']}")
    
    asyncio.run(main())
