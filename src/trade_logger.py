import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

IST = ZoneInfo("Asia/Kolkata")

class TradeLogger:
    """Handles human-readable trade logging to date-based JSON files."""
    def __init__(self, log_dir: str = "logs/trades"):
        self._log_dir = Path(os.getcwd()) / log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("trade_logger")

    def _get_log_path(self) -> Path:
        """Returns the log file path for the current IST day."""
        date_str = datetime.now(IST).strftime("%Y-%m-%d")
        return self._log_dir / f"{date_str}_trades.json"

    def log_event(self, event_type: str, details: dict):
        """Append an event to the daily JSON log file."""
        log_path = self._get_log_path()
        
        timestamp = datetime.now(IST).isoformat()
        log_entry = {
            "timestamp_ist": timestamp,
            "event": event_type,
            "details": details
        }

        # Initialize or load existing logs
        logs = []
        if log_path.exists():
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        logs = json.loads(content)
                        if not isinstance(logs, list):
                            logs = [logs]
            except Exception as e:
                self._logger.error(f"Failed to read existing log file {log_path}: {e}")
                logs = []

        # Append and save
        logs.append(log_entry)
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                json.dump(logs, f, indent=4)
        except Exception as e:
            self._logger.error(f"Failed to write to log file {log_path}: {e}")
