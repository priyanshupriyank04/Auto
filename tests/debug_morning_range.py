import os
import sys
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Add project root to sys.path
sys.path.append(os.getcwd())

IST = ZoneInfo("Asia/Kolkata")
DB_PATH = "data/bot_state.db"

def utc_ms_to_ist_str(ms):
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(IST)
    return dt.strftime("%H:%M:%S")

def is_green(candle):
    return float(candle['close']) > float(candle['open'])

def is_red(candle):
    return float(candle['close']) < float(candle['open'])

def get_color(candle):
    if is_green(candle): return "GREEN 🟢"
    if is_red(candle): return "RED   🔴"
    return "DOJI  ⚪"

def run_debug():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    # Current date for focus
    now_ist = datetime.now(IST)
    target_date = now_ist.strftime("%Y-%m-%d")
    
    print(f"============================================================")
    print(f"MORNING RANGE DEBUGGER (Date: {target_date} IST)")
    print(f"============================================================")
    
    # Fetch all 5m candles for today IST, starting from midnight
    today_start_ist = datetime(now_ist.year, now_ist.month, now_ist.day, 0, 0, 0, tzinfo=IST)
    today_start_ms = int(today_start_ist.astimezone(timezone.utc).timestamp() * 1000)
    
    # Session starts at 08:00 IST
    session_start_ist = datetime(now_ist.year, now_ist.month, now_ist.day, 8, 0, 0, tzinfo=IST)
    session_start_ms = int(session_start_ist.astimezone(timezone.utc).timestamp() * 1000)
    
    query = """
        SELECT * FROM closed_candles 
        WHERE symbol = 'BTC-USDC' AND interval = '5m' AND open_time >= ?
        ORDER BY open_time ASC
    """
    candles = [dict(c) for c in conn.execute(query, (today_start_ms,)).fetchall()]
    
    if not candles:
        print("❌ No candles found in DB for today. Did the bot run today?")
        return

    # Strategy Logic Simulation:
    # 1. Skip everything before 08:00 IST
    # 2. First candle >= 08:00 is 'prev'
    # 3. Next candle is 'curr'
    # 4. If opposite, range found.
    
    idx1, idx2 = -1, -1
    prev_candle = None
    
    for i, c in enumerate(candles):
        if c['open_time'] < session_start_ms:
            continue
        
        if prev_candle is None:
            prev_candle = c
            idx1 = i
            continue
            
        curr_candle = c
        idx2 = i
        
        # Check opposite color
        if (is_green(prev_candle) and is_red(curr_candle)) or (is_red(prev_candle) and is_green(curr_candle)):
            # Found!
            break
        else:
            # Shift
            prev_candle = curr_candle
            idx1 = idx2
            idx2 = -1

    if idx2 == -1:
        print(f"❌ Range defining pair NOT FOUND yet after 08:00 IST.")
        print(f"Last candle seen at: {utc_ms_to_ist_str(candles[-1]['open_time'])} IST")
        return

    c1 = candles[idx1]
    c2 = candles[idx2]
    
    raw_high = max(float(c1['high']), float(c2['high']))
    raw_low = min(float(c1['low']), float(c2['low']))
    bot_high = float(round(raw_high, 0))
    bot_low = float(round(raw_low, 0))
    
    print(f"\n📊 RANGE IDENTIFIED:")
    print(f"------------------------------------------------------------")
    print(f"Candle 1 (Prev): {utc_ms_to_ist_str(c1['open_time'])} | {get_color(c1)} | H:{c1['high']} L:{c1['low']} | Unix:{c1['open_time']}")
    print(f"Candle 2 (Curr): {utc_ms_to_ist_str(c2['open_time'])} | {get_color(c2)} | H:{c2['high']} L:{c2['low']} | Unix:{c2['open_time']}")
    print(f"------------------------------------------------------------")
    print(f"Raw Extremes:   High={raw_high:.2f}, Low={raw_low:.2f}")
    print(f"Bot (Rounded):  High={bot_high}, Low={bot_low} (Size: {bot_high - bot_low})")
    print(f"------------------------------------------------------------")

    print(f"\n🔍 CONTEXT WINDOW (+/- 5 Candles):")
    print(f"ID  | Time IST | Color    | High      | Low       | Close      | Timestamp")
    
    start_view = max(0, idx1 - 5)
    end_view = min(len(candles), idx2 + 6)
    
    for i in range(start_view, end_view):
        c = candles[i]
        marker = ">> " if i == idx1 or i == idx2 else "   "
        row = f"{marker}{c['id']: <3} | {utc_ms_to_ist_str(c['open_time'])}  | {get_color(c): <8} | {c['high']: <9} | {c['low']: <9} | {c['close']: <10} | {c['open_time']}"
        print(row)

    conn.close()

if __name__ == "__main__":
    run_debug()
