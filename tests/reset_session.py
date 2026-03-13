import sqlite3
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# Configuration
DB_PATH = "data/bot_state.db"
COMPONENT_NAME = "breakout_strategy"
IST = ZoneInfo("Asia/Kolkata")

def reset_today_state():
    if not os.path.exists(DB_PATH):
        print(f"No database found at {DB_PATH}")
        return

    # Get today's IST date to confirm what we are deleting
    today_ist = datetime.now(IST).strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    print(f"--- Session Reset Tool ---")
    
    # 1. Check current status
    row = conn.execute("SELECT meta_json FROM component_status WHERE component_name = ?", (COMPONENT_NAME,)).fetchone()
    
    if row:
        print(f"Current stored state found for component: {COMPONENT_NAME}")
        # We don't strictly need to parse it, just wipe the row or update it
    else:
        print(f"No stored state found for {COMPONENT_NAME}. Nothing to reset.")
        conn.close()
        return

    # 2. Delete the state for the strategy
    # This forces the bot to re-run replay and re-scan for the range on next startup
    try:
        conn.execute("DELETE FROM component_status WHERE component_name = ?", (COMPONENT_NAME,))
        
        # Also clear paper trade events for today if you want a clean slate for the debugger
        # (Optional, but keeps logs clean)
        conn.execute("DELETE FROM paper_trade_events WHERE ist_date = ?", (today_ist,))
        conn.execute("DELETE FROM paper_trades WHERE trade_date_ist = ?", (today_ist,))
        conn.execute("DELETE FROM paper_daily_summary WHERE ist_date = ?", (today_ist,))
        
        conn.commit()
        print(f"✅ SUCCESS: Strategy state and trade history for {today_ist} have been cleared.")
        print(f"You can now restart the bot with TEST_MODE = False.")
    except Exception as e:
        print(f"❌ ERROR: Failed to reset state: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    reset_today_state()
