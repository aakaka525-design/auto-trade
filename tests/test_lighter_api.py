"""
Lighter API 测试脚本
在服务器上运行: python test_lighter_api.py
"""
import asyncio
import datetime
import lighter

async def test():
    print("=== Lighter API 测试 ===\n")
    
    for env, host in [
        ("Testnet", "https://testnet.zklighter.elliot.ai"),
        ("Mainnet", "https://mainnet.zklighter.elliot.ai"),
    ]:
        print(f"{env}:")
        client = lighter.ApiClient(configuration=lighter.Configuration(host=host))
        
        # 1. K线
        try:
            api = lighter.CandlestickApi(client)
            now = int(datetime.datetime.now().timestamp())
            r = await api.candlesticks(
                market_id=0, resolution="1h",
                start_timestamp=now - 86400, end_timestamp=now, count_back=5
            )
            print(f"  Candlesticks: ✅ {len(r.candlesticks)} 根")
            if r.candlesticks:
                c = r.candlesticks[-1]
                print(f"    最新: close=${float(c.close):,.2f}")
        except Exception as e:
            print(f"  Candlesticks: ❌ {type(e).__name__}")
        
        # 2. 订单簿
        try:
            api = lighter.OrderApi(client)
            r = await api.order_book_details(market_id=0)
            bids = len(r.bids) if hasattr(r, 'bids') and r.bids else 0
            asks = len(r.asks) if hasattr(r, 'asks') and r.asks else 0
            print(f"  OrderBook: ✅ {bids} bids, {asks} asks")
        except Exception as e:
            print(f"  OrderBook: ❌ {type(e).__name__}")
        
        # 3. 最近成交
        try:
            api = lighter.OrderApi(client)
            r = await api.recent_trades(market_id=0, limit=3)
            print(f"  RecentTrades: ✅ {len(r.trades)} trades")
            if r.trades:
                print(f"    最新价: ${float(r.trades[0].price):,.2f}")
        except Exception as e:
            print(f"  RecentTrades: ❌ {type(e).__name__}")
        
        await client.close()
        print()

if __name__ == "__main__":
    asyncio.run(test())
