"""
Binance 交易对获取模块

提供现货和合约交易对的获取功能。
"""
import aiohttp
import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

# 稳定币后缀列表
STABLECOIN_SUFFIXES = ['USDT', 'USDC', 'USDE', 'USD1', 'TUSD', 'BUSD', 'FDUSD']


async def get_spot_symbols() -> List[dict]:
    """
    获取现货稳定币交易对
    
    使用 24hr ticker API 获取所有活跃交易对
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.binance.com/api/v3/ticker/24hr", timeout=30) as resp:
                if resp.status != 200:
                    logger.error(f"获取现货交易对失败: {resp.status}")
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
        logger.error(f"获取现货交易对异常: {e}")
        return []


async def get_futures_symbols() -> List[dict]:
    """
    获取 U 本位合约交易对
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=30) as resp:
                if resp.status != 200:
                    logger.error(f"获取合约交易对失败: {resp.status}")
                    return []
                
                data = await resp.json()
                pairs = []
                
                for item in data:
                    symbol = item['symbol']
                    # U 本位合约通常以 USDT/USDC 结尾
                    if symbol.endswith('USDT') or symbol.endswith('USDC'):
                        pairs.append({
                            'symbol': symbol.lower(),
                            'volume': float(item.get('quoteVolume', 0)),
                            'market': 'futures'
                        })
                
                logger.info(f"合约: 找到 {len(pairs)} 个交易对")
                return pairs
                
    except Exception as e:
        logger.error(f"获取合约交易对异常: {e}")
        return []


async def get_all_symbols() -> Tuple[List[dict], List[dict]]:
    """
    获取所有交易对 (现货 + 合约)
    
    Returns:
        (spot_pairs, futures_pairs)
    """
    import asyncio
    spot_task = asyncio.create_task(get_spot_symbols())
    futures_task = asyncio.create_task(get_futures_symbols())
    
    spot_pairs, futures_pairs = await asyncio.gather(spot_task, futures_task)
    return spot_pairs, futures_pairs
