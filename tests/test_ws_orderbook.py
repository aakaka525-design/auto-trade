"""
Lighter WebSocket 订单簿测试
运行: python test_ws_orderbook.py
"""
import json
import logging
import lighter

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s')

def on_order_book_update(market_id, order_book):
    """订单簿更新回调"""
    bids = order_book.get('bids', {})
    asks = order_book.get('asks', {})
    
    print(f"\n=== Market {market_id} 订单簿 ===")
    print(f"买单 (Bids): {len(bids)} 档")
    
    # bids/asks 是字典 {price: size}
    sorted_bids = sorted(bids.items(), key=lambda x: float(x[0]), reverse=True)[:5]
    for i, (price, size) in enumerate(sorted_bids):
        print(f"  {i+1}. ${float(price):,.2f} x {size}")
    
    print(f"\n卖单 (Asks): {len(asks)} 档")
    sorted_asks = sorted(asks.items(), key=lambda x: float(x[0]))[:5]
    for i, (price, size) in enumerate(sorted_asks):
        print(f"  {i+1}. ${float(price):,.2f} x {size}")


if __name__ == "__main__":
    print("=== Lighter WebSocket 订单簿测试 ===")
    print("按 Ctrl+C 退出\n")
    
    client = lighter.WsClient(
        order_book_ids=[0],
        account_ids=[],
        on_order_book_update=on_order_book_update,
    )
    
    try:
        client.run()
    except KeyboardInterrupt:
        print("\n已停止")
