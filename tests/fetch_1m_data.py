import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Add project root to path to import client
sys.path.append(os.getcwd())
from src.hyperliquid_client import HyperliquidClient

IST = ZoneInfo("Asia/Kolkata")

def fetch_1m_data(coin="BTC", target_date_ist="2026-03-12"):
    """
    Fetches 1-minute historical candles for a specific IST date using the candleSnapshot API.
    Hyperliquid supports up to 5000 candles lookback for 1m interval (~3.4 days).
    """
    client = HyperliquidClient()
    
    if target_date_ist is None:
        target_date_ist = datetime.now(IST).strftime("%Y-%m-%d")
        
    print(f"--- Hyperliquid 1m Data Fetcher ---")
    print(f"Target Coin: {coin}")
    print(f"Target Date (IST): {target_date_ist}")

    try:
        # 1. Calculate the time range for the target date in IST
        target_dt = datetime.strptime(target_date_ist, "%Y-%m-%d").replace(tzinfo=IST)
        
        # Session start (00:00:00 IST) and end (23:59:59 IST)
        start_dt = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = start_dt + timedelta(days=1) - timedelta(milliseconds=1)
        
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        # 2. Fetch candles from API
        # get_candles(symbol, interval, start_ms, end_ms)
        candles = client.get_candles(f"{coin}-USDC", "1m", start_ms, end_ms)
        
        if not candles:
            print(f"No 1m candles found for {target_date_ist}. (Note: API limit is ~3.5 days lookback)")
            return

        print(f"Fetched {len(candles)} candles.")

        # 3. Format for CSV
        csv_rows = []
        for c in candles:
            ts_ms = c.get("open_time")
            dt_ist = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).astimezone(IST)
            
            csv_rows.append({
                "timestamp_ms": ts_ms,
                "time_ist": dt_ist.strftime("%Y-%m-%d %H:%M:%S"),
                "open": c.get("open"),
                "high": c.get("high"),
                "low": c.get("low"),
                "close": c.get("close"),
                "volume": c.get("volume")
            })

        # 4. Save to CSV in tests directory
        filename = f"tests/{coin}_{target_date_ist}_1m_data.csv"
        fieldnames = ["timestamp_ms", "time_ist", "open", "high", "low", "close", "volume"]
        
        # Sort by timestamp
        csv_rows.sort(key=lambda x: x["timestamp_ms"])
        
        with open(filename, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
            
        print(f"✅ SUCCESS: Saved {len(csv_rows)} minutes of data to {filename}")

    except Exception as e:
        print(f"❌ Error fetching data: {e}")

if __name__ == "__main__":
    # You can change the date here explicitly if needed e.g. "2026-03-12"
    fetch_1m_data("BTC")
