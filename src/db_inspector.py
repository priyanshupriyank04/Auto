"""
Read-only database inspector for persisted bot state.

This module provides DBInspector for:
- Inspecting SQLite contents (row counts, tables)
- Printing latest market snapshot, component statuses, runner stats
- Printing recent 1m and 5m closed candles
- Validating candle sequence (duplicates, gaps, order)

Read-only; no strategy, websocket, or order logic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.state_store import StateStore

# Runner name used by main_readonly
RUNNER_NAME = "main_readonly"

# Expected step in ms for candle intervals
STEP_1M_MS = 60_000
STEP_5M_MS = 300_000


class DBInspector:
    """
    Read-only inspector for bot state SQLite database.
    Prints and validates persisted state; does not modify data.
    """

    def __init__(self, db_path: str = "data/bot_state.db", symbol: str = "BTC-USDC") -> None:
        """
        Initialize the inspector.

        Args:
            db_path: Path to SQLite database file.
            symbol: Symbol to filter snapshots and candles (e.g. BTC-USDC).
        """
        self._db_path = Path(db_path).resolve()
        self._symbol = symbol
        self._store: StateStore | None = None

    def _ensure_connected(self) -> StateStore:
        """Connect to DB if not already; raise if DB missing or invalid."""
        if self._store is not None:
            return self._store
        if not self._db_path.exists():
            raise FileNotFoundError(f"Database not found: {self._db_path}")
        self._store = StateStore(str(self._db_path))
        return self._store

    def _safe_connect(self) -> StateStore | None:
        """Connect to DB; return None if missing or error."""
        try:
            return self._ensure_connected()
        except Exception:
            return None

    def print_db_info(self) -> None:
        """Print DB path and row counts per table."""
        store = self._safe_connect()
        if store is None:
            print("[DBInspector] Database not found or inaccessible.")
            return
        info = store.get_db_info()
        print("\n" + "=" * 60)
        print("DATABASE INFO")
        print("=" * 60)
        print(f"  Path: {info['db_path']}")
        print("  Row counts:")
        for table, count in info["counts"].items():
            print(f"    {table}: {count}")
        print("=" * 60 + "\n")

    def print_component_statuses(self) -> None:
        """Print all component_status rows ordered by component name."""
        store = self._safe_connect()
        if store is None:
            print("[DBInspector] Database not found or inaccessible.")
            return
        conn = store._conn
        sql = """
            SELECT component_name, status, last_update_ts, meta_json
            FROM component_status
            ORDER BY component_name
        """
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as e:
            print(f"[DBInspector] Error reading component_status: {e}")
            return
        print("\n" + "=" * 60)
        print("COMPONENT STATUSES")
        print("=" * 60)
        if not rows:
            print("  (no rows)")
        else:
            for row in rows:
                d = dict(row)
                meta = d.pop("meta_json", None)
                meta_str = ""
                if meta:
                    try:
                        m = json.loads(meta) if isinstance(meta, str) else meta
                        meta_str = f"    meta: {m}"
                    except (TypeError, ValueError):
                        meta_str = f"    meta: (raw) {meta}"
                print(f"  {d.get('component_name', '?')}: {d.get('status', '?')}")
                print(f"    last_update_ts: {d.get('last_update_ts')}")
                if meta_str:
                    print(meta_str)
        print("=" * 60 + "\n")

    def print_latest_snapshot(self) -> None:
        """Print latest market snapshot for the configured symbol."""
        store = self._safe_connect()
        if store is None:
            print("[DBInspector] Database not found or inaccessible.")
            return
        snap = store.get_latest_market_snapshot(self._symbol)
        print("\n" + "=" * 60)
        print(f"LATEST MARKET SNAPSHOT ({self._symbol})")
        print("=" * 60)
        if snap is None:
            print("  (no snapshot)")
        else:
            for k, v in snap.items():
                if k != "raw":
                    print(f"  {k}: {v}")
            raw = snap.get("raw")
            if raw is not None:
                try:
                    raw_str = json.dumps(raw, default=str)
                except (TypeError, ValueError):
                    raw_str = str(raw)
                if len(raw_str) > 200:
                    print(f"  raw: {raw_str[:200]}... (truncated)")
                else:
                    print(f"  raw: {raw_str}")
        print("=" * 60 + "\n")

    def print_runner_stats(self) -> None:
        """Print runner stats for main_readonly."""
        store = self._safe_connect()
        if store is None:
            print("[DBInspector] Database not found or inaccessible.")
            return
        stats = store.get_runner_stats(RUNNER_NAME)
        print("\n" + "=" * 60)
        print(f"RUNNER STATS ({RUNNER_NAME})")
        print("=" * 60)
        if stats is None:
            print("  (no stats)")
        else:
            for k, v in stats.items():
                print(f"  {k}: {v}")
        print("=" * 60 + "\n")

    def print_recent_candles(self, interval: str, limit: int = 10) -> None:
        """Print latest candles for the given interval."""
        store = self._safe_connect()
        if store is None:
            print("[DBInspector] Database not found or inaccessible.")
            return
        candles = store.get_recent_closed_candles(self._symbol, interval, limit=limit)
        print("\n" + "=" * 60)
        print(f"RECENT {interval.upper()} CANDLES ({self._symbol})")
        print("=" * 60)
        if not candles:
            print("  (no candles)")
        else:
            for c in candles:
                print(
                    f"  open_time={c.get('open_time')} close_time={c.get('close_time')} "
                    f"o={c.get('open')} h={c.get('high')} l={c.get('low')} c={c.get('close')} "
                    f"ticks={c.get('tick_count')}"
                )
        print("=" * 60 + "\n")

    def validate_candle_sequence(
        self, interval: str, expected_step_ms: int
    ) -> dict[str, Any]:
        """
        Inspect recent candles and report validation results.

        Args:
            interval: Candle interval (e.g. 1m, 5m).
            expected_step_ms: Expected step between open_time values (60000 for 1m, 300000 for 5m).

        Returns:
            Dict with: total_checked, duplicate_open_times, backward_order_issues,
            gap_count, first_open_time, last_open_time.
        """
        result: dict[str, Any] = {
            "total_checked": 0,
            "duplicate_open_times": [],
            "backward_order_issues": [],
            "gap_count": 0,
            "first_open_time": None,
            "last_open_time": None,
        }
        store = self._safe_connect()
        if store is None:
            return result

        # Fetch more candles for validation; order ASC for sequence check
        sql = """
            SELECT open_time, close_time, open, high, low, close, tick_count
            FROM closed_candles
            WHERE symbol = ? AND interval = ?
            ORDER BY open_time ASC
            LIMIT 500
        """
        try:
            rows = store._conn.execute(
                sql, (self._symbol, interval)
            ).fetchall()
        except sqlite3.Error:
            return result

        candles = [dict(r) for r in rows]
        result["total_checked"] = len(candles)

        if not candles:
            return result

        seen_open_times: set[int] = set()
        prev_open: int | None = None

        for c in candles:
            ot = c.get("open_time")
            if ot is None:
                continue
            try:
                ot_int = int(ot)
            except (TypeError, ValueError):
                continue

            if ot_int in seen_open_times:
                result["duplicate_open_times"].append(ot_int)
                continue  # Don't update prev_open; duplicate is not a gap
            seen_open_times.add(ot_int)

            if prev_open is not None:
                if ot_int < prev_open:
                    result["backward_order_issues"].append(
                        {"prev": prev_open, "current": ot_int}
                    )
                elif ot_int - prev_open != expected_step_ms:
                    result["gap_count"] += 1

            prev_open = ot_int

        if candles:
            first_ot = candles[0].get("open_time")
            last_ot = candles[-1].get("open_time")
            if first_ot is not None:
                result["first_open_time"] = int(first_ot)
            if last_ot is not None:
                result["last_open_time"] = int(last_ot)

        return result

    def run_full_report(self) -> None:
        """Call all print methods and validation in a clean order."""
        self.print_db_info()
        self.print_component_statuses()
        self.print_latest_snapshot()
        self.print_runner_stats()
        self.print_recent_candles("1m", limit=10)
        self.print_recent_candles("5m", limit=10)

        print("\n" + "=" * 60)
        print("CANDLE VALIDATION")
        print("=" * 60)
        v1 = self.validate_candle_sequence("1m", STEP_1M_MS)
        print(f"  1m: total_checked={v1['total_checked']} duplicates={len(v1['duplicate_open_times'])} "
              f"backward_issues={len(v1['backward_order_issues'])} gaps={v1['gap_count']} "
              f"first={v1['first_open_time']} last={v1['last_open_time']}")
        v5 = self.validate_candle_sequence("5m", STEP_5M_MS)
        print(f"  5m: total_checked={v5['total_checked']} duplicates={len(v5['duplicate_open_times'])} "
              f"backward_issues={len(v5['backward_order_issues'])} gaps={v5['gap_count']} "
              f"first={v5['first_open_time']} last={v5['last_open_time']}")
        print("=" * 60 + "\n")

    def close(self) -> None:
        """Close the StateStore connection if open."""
        if self._store is not None:
            try:
                self._store.close()
            except Exception:
                pass
            self._store = None


if __name__ == "__main__":
    import logging
    logging.getLogger("src.state_store").setLevel(logging.WARNING)
    inspector = DBInspector(db_path="data/bot_state.db", symbol="BTC-USDC")
    try:
        inspector.run_full_report()
    except FileNotFoundError as e:
        print(f"[DBInspector] {e}")
        print("Run 'python -m src.main_readonly' first to create and populate the database.")
    except Exception as e:
        print(f"[DBInspector] Error: {e}")
        raise
    finally:
        inspector.close()
