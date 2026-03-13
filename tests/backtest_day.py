import csv
import json
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

# Add project root to path
sys.path.append(os.getcwd())
from src.breakout_strategy import BreakoutStrategyEngine

IST = ZoneInfo("Asia/Kolkata")

class BacktestLogger:
    """Special logger for backtests that writes to the tests directory."""
    def __init__(self, target_date, output_dir="tests"):
        self.date_str = target_date
        self.output_dir = Path(output_dir)
        self.pnl_path = self.output_dir / f"backtest_pnl_{target_date}.csv"
        self.trade_path = self.output_dir / f"backtest_trades_{target_date}.json"
        
        # Init PnL Header
        with open(self.pnl_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time_ist", "symbol", "direction", "entry_price", "exit_price", "size_btc", "exit_reason", "pnl_usd", "pnl_pct"])

        self.trade_events = []

    def log_trade(self, time_ist, symbol, direction, entry, exit, size, reason):
        if direction == "long":
            pnl_usd = (exit - entry) * size
            pnl_pct = (exit - entry) / entry * 100
        else:
            pnl_usd = (entry - exit) * size
            pnl_pct = (entry - exit) / entry * 100
            
        with open(self.pnl_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([time_ist, symbol, direction, f"{entry:.2f}", f"{exit:.2f}", size, reason, f"{pnl_usd:.4f}", f"{pnl_pct:.4f}"])
        
        print(f"💰 BACKTEST TRADE: {direction.upper()} {reason.upper()} | PnL: ${pnl_usd:.2f} ({pnl_pct:.2f}%)")

    def log_event(self, time_ist, event_type, details):
        self.trade_events.append({
            "timestamp_ist": time_ist,
            "event": event_type,
            "details": details
        })
        # Save JSON on every event to be safe
        with open(self.trade_path, "w") as f:
            json.dump(self.trade_events, f, indent=4)

def run_backtest(csv_path):
    if not os.path.exists(csv_path):
        print(f"Error: CSV file not found at {csv_path}")
        return

    # 1. Parse filename for date and coin
    # Format expected: tests/BTC_2026-03-12_1m_data.csv
    base = os.path.basename(csv_path)
    parts = base.split("_")
    coin = parts[0]
    target_date = parts[1]
    
    print(f"--- Starting Backtest Simulation ---")
    print(f"Coin: {coin} | Date: {target_date}")
    
    logger = BacktestLogger(target_date)
    
    # Use a temporary DB for the backtest so we don't pollute live state
    test_db = f"tests/backtest_{target_date}.db"
    if os.path.exists(test_db):
        os.remove(test_db)
        
    engine = BreakoutStrategyEngine(symbol=f"{coin}-USDC", db_path=test_db, target_date=target_date)
    
    # 2. Load 1m candles from CSV
    m1_candles = []
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            m1_candles.append({
                "ts": int(row["timestamp_ms"]),
                "time_ist": row["time_ist"],
                "o": float(row["open"]),
                "h": float(row["high"]),
                "l": float(row["low"]),
                "c": float(row["close"]),
                "v": float(row["volume"])
            })
    
    # Sort just in case
    m1_candles.sort(key=lambda x: x["ts"])
    
    # 3. Simulation Variables
    current_entry = None
    current_side = None
    current_size = None
    
    # 5m aggregation buffer
    buffer_5m = []
    
    print("Simulating minute-by-minute...")
    
    for i, c in enumerate(m1_candles):
        ts = c["ts"]
        time_ist = c["time_ist"]
        
        # A. Aggregate 5m candles
        buffer_5m.append(c)
        
        # Check if this 1m candle is the end of a 5m boundary (0, 5, 10, 15...)
        # Note: Hyperliquid candles are at 00, 05, 10...
        dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).astimezone(IST)
        
        # B. If 5m candle closes, feed to strategy
        if dt.minute % 5 == 4: # This 1m candle (e.g. 08:09) finishes the 5m bar (08:05-08:10)
            if len(buffer_5m) >= 5:
                # Calculate close time (1 min after the open time of the 9th minute)
                close_time_ms = ts + 60_000
                close_time_ist = datetime.fromtimestamp(close_time_ms / 1000.0, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")

                candle_5m = {
                    "open_time": buffer_5m[0]["ts"],
                    "open": buffer_5m[0]["o"],
                    "high": max(x["h"] for x in buffer_5m),
                    "low": min(x["l"] for x in buffer_5m),
                    "close": buffer_5m[-1]["c"],
                    "is_closed": True
                }
                res_5m = engine.process_closed_5m_candle(candle_5m)
                if res_5m.get("pair_found"):
                    logger.log_event(close_time_ist, "range_identified", {
                        "high": res_5m["range_high"],
                        "low": res_5m["range_low"],
                        "size": res_5m["range_size"]
                    })
                    print(f"📦 Range Identified: [{res_5m['range_high']} - {res_5m['range_low']}] at {close_time_ist}")
                buffer_5m = [] # Clear for next 5m
        
        # C. Process "Live" price updates using 1m candle phases
        # We simulate: Open -> High -> Low -> Close to catch SL/TP correctly within the minute
        prices_to_check = [c["o"], c["h"], c["l"], c["c"]]
        
        for p in prices_to_check:
            res = engine.process_live_price(p, ts)
            
            # Handle Entry Signal
            if res.get("entry"):
                entry_data = res["entry"]
                current_entry = entry_data["price"]
                current_side = "long" if entry_data["signal"] == "long_entry" else "short"
                current_size = entry_data["size"]
                logger.log_event(time_ist, "trade_entry", {
                    "side": current_side,
                    "price": current_entry,
                    "size": current_size,
                    "sl": entry_data["sl"],
                    "tp": entry_data["tp"]
                })
                print(f"🚀 {current_side.upper()} ENTRY at {current_entry} ({time_ist})")

            # Handle Exit Signal
            if res.get("exit"):
                exit_data = res["exit"]
                exit_price = exit_data["price"]
                exit_reason = exit_data["exit"]
                
                logger.log_trade(
                    time_ist=time_ist,
                    symbol=f"{coin}-USDC",
                    direction=current_side,
                    entry=current_entry,
                    exit=exit_price,
                    size=current_size,
                    reason=exit_reason
                )
                logger.log_event(time_ist, "trade_exit", {
                    "side": current_side,
                    "exit_type": exit_reason,
                    "price": exit_price,
                    "entry_price": current_entry
                })
                
                # Reset simulation tracking
                current_entry = None
                current_side = None
                current_size = None

    print(f"\n--- Backtest Finished ---")
    print(f"PnL File: tests/backtest_pnl_{target_date}.csv")
    print(f"Trades File: tests/backtest_trades_{target_date}.json")
    
    if os.path.exists(test_db):
        os.remove(test_db)

if __name__ == "__main__":
    # Default to the file you just fetched if it exists
    # You can pass a specific file path as an argument too
    target_csv = "tests/BTC_2026-03-12_1m_data.csv"
    if len(sys.argv) > 1:
        target_csv = sys.argv[1]
        
    run_backtest(target_csv)
