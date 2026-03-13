import pandas as pd
import mplfinance as mpf
import json
import os
import sys
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

def generate_png_charts(date_str, coin="BTC"):
    project_root = Path(__file__).parent.parent
    csv_path = project_root / f"tests/{coin}_{date_str}_1m_data.csv"
    json_path = project_root / f"tests/backtest_trades_{date_str}.json"
    output_dir = project_root / "tests/charts"
    output_dir.mkdir(parents=True, exist_ok=True)

    if not csv_path.exists() or not json_path.exists():
        print(f"Error: Required files for {date_str} not found in tests/.")
        return

    # 1. Load Data
    df = pd.read_csv(csv_path)
    df['timestamp'] = pd.to_datetime(df['timestamp_ms'], unit='ms', utc=True).dt.tz_convert(IST)
    df.set_index('timestamp', inplace=True)
    
    with open(json_path, "r") as f:
        trades = json.load(f)

    # 2. Extract Trade Pairs
    trade_pairs = []
    current_entry = None
    range_info = None

    for event in trades:
        if event["event"] == "range_identified":
            range_info = event["details"]
        elif event["event"] == "trade_entry":
            current_entry = event
        elif event["event"] == "trade_exit" and current_entry:
            trade_pairs.append({
                "entry": current_entry,
                "exit": event,
                "range": range_info
            })
            current_entry = None

    print(f"Generating {len(trade_pairs)} PNG charts for {date_str}...")

    # 3. Plot each trade
    for i, pair in enumerate(trade_pairs):
        try:
            entry_time = pd.to_datetime(pair["entry"]["timestamp_ist"]).replace(tzinfo=IST)
            exit_time = pd.to_datetime(pair["exit"]["timestamp_ist"]).replace(tzinfo=IST)
            
            # Padding: 15 mins before/after
            start_plot = entry_time - pd.Timedelta(minutes=15)
            end_plot = exit_time + pd.Timedelta(minutes=15)
            
            # Slice dataframe
            plot_df = df.loc[start_plot:end_plot].copy()
            if plot_df.empty:
                print(f"Warning: No data to plot for Trade #{i+1}")
                continue

            # Markers
            # Create a series for markers (mostly NaNs)
            entry_series = pd.Series(index=plot_df.index, dtype=float)
            exit_series = pd.Series(index=plot_df.index, dtype=float)
            
            # Find closest index for markers
            entry_idx = plot_df.index.get_indexer([entry_time], method='nearest')[0]
            exit_idx = plot_df.index.get_indexer([exit_time], method='nearest')[0]
            
            entry_series.iloc[entry_idx] = pair["entry"]["details"]["price"]
            exit_series.iloc[exit_idx] = pair["exit"]["details"]["price"]

            # Define markers for mplfinance
            apd = [
                mpf.make_addplot(entry_series, type='scatter', markersize=100, marker='^', color='cyan'),
                mpf.make_addplot(exit_series, type='scatter', markersize=100, marker='v', color='magenta')
            ]

            # Range Lines (Horizontal)
            hlines = dict(hlines=[pair["range"]["high"], pair["range"]["low"]],
                         colors=['yellow', 'yellow'],
                         linewidths=[1, 1],
                         alpha=0.6)

            # Chart Title
            title = f"Trade #{i+1} | {pair['entry']['details']['side'].upper()} | {pair['exit']['details']['exit_type'].upper()}"
            
            # Save to File
            save_path = output_dir / f"trade_{i+1}_{date_str}.png"
            
            mpf.plot(plot_df, 
                     type='candle', 
                     addplot=apd, 
                     hlines=hlines,
                     style='charles', 
                     title=title,
                     ylabel='Price (USDC)',
                     savefig=dict(fname=save_path, dpi=150, bbox_inches='tight'),
                     volume=True,
                     tight_layout=True)
            
            print(f"✅ Created: {save_path.name}")
        except Exception as e:
            print(f"❌ Failed to create chart {i+1}: {e}")

if __name__ == "__main__":
    date = "2026-03-12"
    if len(sys.argv) > 1:
        date = sys.argv[1]
    
    # Check dependencies
    try:
        import pandas
        import mplfinance
    except ImportError:
        print("\n❌ MISSING DEPENDENCIES!")
        print("Please run: pip install pandas mplfinance matplotlib")
        sys.exit(1)
        
    generate_png_charts(date)
