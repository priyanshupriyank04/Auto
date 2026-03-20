import sqlite3
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Add project root to sys.path so we can import src
sys.path.append(os.getcwd())
from src.hyperliquid_client import HyperliquidClient

# Configuration
DB_PATH = "data/bot_state.db"
COMPONENT_NAME_PREFIX = "breakout_strategy"
IST = ZoneInfo("Asia/Kolkata")

def close_all_hyperliquid_positions():
    print("Checking Hyperliquid for open positions...")
    try:
        client = HyperliquidClient()
        positions = client.get_positions()
        
        btc_pos = next((p for p in positions if p["symbol"] == "BTC-USDC"), None)
        if btc_pos:
            raw_size = float(btc_pos["size"])
            size = abs(raw_size)
            if size > 0:
                side = "buy" if raw_size < 0 else "sell"
                print(f"Found active position: {raw_size} BTC-USDC. Executing MARKET CLOSE...")
                
                # Fetch current price to use as reference for the market order
                ticker = client.get_ticker("BTC-USDC")
                exit_price = float(ticker.get("last_price", 0))
                
                resp = client.place_order(
                    symbol="BTC-USDC",
                    side=side,
                    order_type="market",
                    qty=size,
                    price=exit_price,
                    reduce_only=True
                )
                print(f"✅ Trade Closed Successfully: {resp}")
            else:
                print("No active BTC-USDC position size to close.")
        else:
            print("No active BTC-USDC position found on exchange.")
    except Exception as e:
        print(f"❌ ERROR: Failed to close positions on Hyperliquid: {e}")

def reset_today_state():
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}")
        return

    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    print(f"--- Session Reset Tool ---")
    
    # 1. Close Live Exchange Trades
    close_all_hyperliquid_positions()
    
    print(f"\nCleaning internal database state...")
    
    # 2. Check current status
    rows = conn.execute("SELECT component_name FROM component_status WHERE component_name LIKE ?", (f"{COMPONENT_NAME_PREFIX}%",)).fetchall()
    
    if rows:
        print(f"Found stored states for: {[r['component_name'] for r in rows]}")
    else:
        print(f"No stored internal state found. Nothing to wipe.")
        conn.close()
        return

    # 3. Delete the state for the strategy
    try:
        conn.execute("DELETE FROM component_status WHERE component_name LIKE ?", (f"{COMPONENT_NAME_PREFIX}%",))
        
        # Clear paper trade events for today
        conn.execute("DELETE FROM paper_trade_events WHERE ist_date = ?", (today_ist,))
        conn.execute("DELETE FROM paper_trades WHERE trade_date_ist = ?", (today_ist,))
        conn.execute("DELETE FROM paper_daily_summary WHERE ist_date = ?", (today_ist,))
        
        conn.commit()
        print(f"✅ SUCCESS: Strategy state and trade history have been completely wiped.")
        print(f"You can now restart your live or paper bot from a clean slate.")
    except Exception as e:
        print(f"❌ ERROR: Failed to reset state: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    reset_today_state()

