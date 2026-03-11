"""
PnL CSV Logger — Records every trade with entry, exit, reason, and profit/loss.
Output: logs/pnl.csv
"""
import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PNL_DIR = "logs"
PNL_FILE = os.path.join(PNL_DIR, "pnl.csv")

HEADERS = [
    "date", "time_ist", "symbol", "direction",
    "entry_price", "exit_price", "size_btc",
    "exit_reason", "pnl_usd", "pnl_pct", "notes"
]


class PnLLogger:
    """Append-only CSV logger for completed trades."""

    def __init__(self, path: str = PNL_FILE):
        self._path = path
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        # Write header if file is new
        if not os.path.exists(self._path) or os.path.getsize(self._path) == 0:
            with open(self._path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(HEADERS)

    def log_trade(
        self,
        symbol: str,
        direction: str,       # "long" or "short"
        entry_price: float,
        exit_price: float,
        size_btc: float,
        exit_reason: str,      # "tp", "sl", "session_end", "manual"
        notes: str = "",
    ) -> dict:
        """
        Log a completed trade to the CSV.
        Returns a dict with the calculated PnL.
        """
        now_ist = datetime.now(IST)

        # Calculate PnL
        if direction == "long":
            pnl_usd = (exit_price - entry_price) * size_btc
        else:  # short
            pnl_usd = (entry_price - exit_price) * size_btc

        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0.0
        if direction == "short":
            pnl_pct = -pnl_pct  # Invert for shorts

        row = [
            now_ist.strftime("%Y-%m-%d"),
            now_ist.strftime("%H:%M:%S"),
            symbol,
            direction,
            f"{entry_price:.2f}",
            f"{exit_price:.2f}",
            f"{size_btc}",
            exit_reason,
            f"{pnl_usd:.4f}",
            f"{pnl_pct:.4f}",
            notes,
        ]

        with open(self._path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(row)

        return {
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 4),
            "direction": direction,
            "exit_reason": exit_reason,
        }
