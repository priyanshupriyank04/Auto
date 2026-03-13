"""
PnL CSV Logger — Records every trade with entry, exit, reason, and profit/loss.
Output: logs/pnl_YYYY-MM-DD.csv
"""
import csv
import os
from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PNL_DIR = "logs"

HEADERS = [
    "date", "time_ist", "symbol", "direction",
    "entry_price", "exit_price", "size_btc",
    "exit_reason", "pnl_usd", "pnl_pct", "notes"
]


class PnLLogger:
    """Append-only CSV logger for completed trades with daily files."""

    def __init__(self, log_dir: str = PNL_DIR):
        self._log_dir = log_dir
        os.makedirs(self._log_dir, exist_ok=True)

    def _get_path(self) -> str:
        """Returns the CSV path for the current IST day."""
        date_str = datetime.now(IST).strftime("%Y-%m-%d")
        return os.path.join(self._log_dir, f"pnl_{date_str}.csv")

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
        Log a completed trade to a daily CSV.
        Returns a dict with the calculated PnL.
        """
        now_ist = datetime.now(IST)
        path = self._get_path()

        # Write header if file is new or empty
        if not os.path.exists(path) or os.stat(path).st_size == 0:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(HEADERS)

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

        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(row)

        return {
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 4),
            "direction": direction,
            "exit_reason": exit_reason,
        }
