#!/usr/bin/env python3
"""
Binance 订单监控功能测试

测试 WebSocket 订单簿和成交流功能。
"""
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import logging
from datetime import datetime

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


async def test_orderbook_stream(symbol: str = "BTC-USDT", duration: int = 10):
    """测试订单簿实时流"""
    from connectors.binance.ws_streams import BinanceWebSocketManager
    
    print(f"\n{'='*60}")
    print(f"📊 测试订单簿流: {symbol}")
    print(f"{'='*60}")
    
    ws = BinanceWebSocketManager(testnet=False)
    
    try:
        if not await ws.connect():
            print("❌ WebSocket 连接失败")
            return
        
        print("✅ WebSocket 已连接")
        
        count = 0
        start_time = asyncio.get_event_loop().time()
        
        async for orderbook in ws.stream_depth(symbol, depth=5):
            count += 1
            
            best_bid = orderbook.best_bid
            best_ask = orderbook.best_ask
            spread = best_ask - best_bid if best_ask and best_bid else 0
            spread_pct = (spread / best_bid * 100) if best_bid else 0
            
            print(f"\n[{count}] {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
            print(f"  买一: {best_bid:,.2f}  |  卖一: {best_ask:,.2f}")
            print(f"  价差: ${spread:.2f} ({spread_pct:.4f}%)")
            print(f"  深度: Bids={len(orderbook.bids)}, Asks={len(orderbook.asks)}")
            
            # 运行指定时间
            if asyncio.get_event_loop().time() - start_time > duration:
                break
        
        print(f"\n✅ 收到 {count} 条订单簿更新")
        
    finally:
        await ws.disconnect()


async def test_trade_stream(symbol: str = "BTC-USDT", duration: int = 10):
    """测试成交流"""
    from connectors.binance.ws_streams import BinanceWebSocketManager
    
    print(f"\n{'='*60}")
    print(f"🔄 测试成交流: {symbol}")
    print(f"{'='*60}")
    
    ws = BinanceWebSocketManager(testnet=False)
    
    try:
        if not await ws.connect():
            print("❌ WebSocket 连接失败")
            return
        
        print("✅ WebSocket 已连接，等待成交...")
        
        count = 0
        total_volume = 0.0
        start_time = asyncio.get_event_loop().time()
        
        async for trade in ws.stream_agg_trades(symbol):
            count += 1
            total_volume += trade.size * trade.price
            
            side_icon = "🟢" if trade.side.value == "BUY" else "🔴"
            print(f"{side_icon} {trade.price:,.2f} x {trade.size:.4f} = ${trade.size * trade.price:,.2f}")
            
            if asyncio.get_event_loop().time() - start_time > duration:
                break
        
        print(f"\n✅ 收到 {count} 笔成交，总成交额: ${total_volume:,.2f}")
        
    finally:
        await ws.disconnect()


async def test_connector_stream(symbol: str = "ETH-USDC", duration: int = 10):
    """通过 BinanceConnector 测试"""
    from connectors import BinanceConnector
    
    print(f"\n{'='*60}")
    print(f"🔌 通过 BinanceConnector 测试: {symbol}")
    print(f"{'='*60}")
    
    connector = BinanceConnector({
        "testnet": False
    })
    
    try:
        if not await connector.connect():
            print("❌ 连接失败")
            return
        
        print("✅ 连接成功，开始接收订单簿...")
        
        count = 0
        start_time = asyncio.get_event_loop().time()
        
        async for orderbook in connector.stream_orderbook(symbol, depth=5):
            count += 1
            print(f"[{count}] Bid: {orderbook.best_bid:,.2f} | Ask: {orderbook.best_ask:,.2f}")
            
            if asyncio.get_event_loop().time() - start_time > duration:
                break
        
        print(f"\n✅ 测试完成，收到 {count} 条更新")
        
    finally:
        await connector.disconnect()


async def main():
    """运行所有测试"""
    print("\n" + "="*60)
    print("🧪 BINANCE 订单监控功能测试")
    print("="*60)
    
    # 测试订单簿流
    await test_orderbook_stream("BTC-USDT", duration=8)
    
    # 测试成交流
    await test_trade_stream("BTC-USDT", duration=8)
    
    # 通过连接器测试 (自动符号转换)
    await test_connector_stream("ETH-USDC", duration=8)
    
    print("\n" + "="*60)
    print("✅ 所有测试完成!")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
