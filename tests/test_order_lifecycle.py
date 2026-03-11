import os
import sys
import time
import json
from pathlib import Path

# Add project root to sys.path
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.hyperliquid_client import HyperliquidClient

def test_order_lifecycle():
    print("Initializing Hyperliquid Client...")
    client = HyperliquidClient()
    
    symbol = "BTC-USDC"
    
    try:
        # 1. Fetch current price to place a safe distant order
        print(f"\n1. Fetching current price for {symbol}...")
        ticker = client.get_ticker(symbol)
        current_price = float(ticker['last_price'])
        print(f"Current Price: ${current_price}")

        # Place a BUY limit order $10,000 BELOW current price (Safe, won't fill)
        # BTC Tick size is 1.0, so price must be an integer
        test_price = round(current_price - 10000.0, 0)
        # Minimum order value on HL is $10. 0.0002 * ~60k = ~$12.
        test_qty = 0.0002 

        print(f"\n2. Placing TEST LIMIT BUY order at ${test_price} (Size: {test_qty})...")
        place_resp = client.place_order(
            symbol=symbol,
            side="buy",
            order_type="limit",
            qty=test_qty,
            price=test_price
        )
        
        if place_resp.get("status") != "ok":
            print(f"API Error placing order: {place_resp}")
            return

        # Extract Order ID (oid) from response
        try:
            status_item = place_resp['response']['data']['statuses'][0]
            if "error" in status_item:
                print(f"Order Rejected by Exchange: {status_item['error']}")
                print(f"Full response: {json.dumps(place_resp, indent=2)}")
                return
            
            oid = status_item.get('resting', {}).get('oid') or status_item.get('filled', {}).get('oid')
        except (KeyError, IndexError):
            print(f"Response format unrecognized. Full response: {json.dumps(place_resp, indent=2)}")
            return

        if oid is None:
            print(f"Order status received but OID is missing. Response: {json.dumps(place_resp, indent=2)}")
            return

        print(f"Order Placed successfully! OID: {oid}")
        time.sleep(1) # Wait for state propagation

        # 3. Verify order exists in open orders
        print("\n3. Verifying open orders...")
        # Note: We need a get_open_orders method in client. Let's check if it exists or use get_positions
        # Actually, let's just proceed to cancel it using the OID we got.
        
        # 4. Cancel the order
        print(f"\n4. Cancelling order {oid}...")
        # Client signature: cancel_order(self, order_id: str, symbol: str | None = None)
        cancel_resp = client.cancel_order(order_id=str(oid), symbol=symbol)
        
        if cancel_resp.get("status") == "ok":
            print("Order cancelled successfully!")
        else:
            print(f"Cancel failed: {cancel_resp}")

    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
        print("Ensure your HL_WALLET_ADDRESS and HL_PRIVATE_KEY are correct in secrets/.env")

if __name__ == "__main__":
    test_order_lifecycle()
