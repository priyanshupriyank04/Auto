"""
SQLite-backed state persistence for a Windows-based Hyperliquid trading bot.

This module provides StateStore for:
- Component status
- Latest market snapshot
- Closed candles
- Runner stats

No strategy, websocket, order placement, candle-building, or risk logic.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StateStore:
    """
    Manages SQLite persistence for bot state: component status, market snapshot,
    closed candles, and runner stats. Windows-friendly paths; dict-like row access.
    """

    def __init__(self, db_path: str = "data/bot_state.db") -> None:
        """
        Initialize StateStore: ensure parent dir exists, connect to SQLite,
        set row_factory for dict-like access, create tables and indexes.
        """
        if db_path == ":memory:":
            self._db_path = db_path
        else:
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._db_path = path.resolve().as_posix()

        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        self._create_indexes()
        logger.info("StateStore initialized: db=%s", self._db_path)

    def close(self) -> None:
        """Close the SQLite connection cleanly."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.debug("StateStore connection closed")

    def get_connection(self) -> sqlite3.Connection:
        """
        Return the underlying SQLite connection.

        Intended for internal components (e.g., paper trade logger) that need
        to share the same DB and benefit from StateStore's schema management.
        """
        if self._conn is None:
            raise RuntimeError("StateStore connection is closed")
        return self._conn

    def upsert_component_status(
        self,
        component_name: str,
        status: str,
        last_update_ts: int,
        meta: dict | None = None,
    ) -> None:
        """
        Insert or update component status. meta is stored as JSON.
        """
        meta_str = self._serialize_json(meta)
        sql = """
            INSERT INTO component_status (component_name, status, last_update_ts, meta_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(component_name) DO UPDATE SET
                status = excluded.status,
                last_update_ts = excluded.last_update_ts,
                meta_json = excluded.meta_json
        """
        try:
            self._conn.execute(sql, (component_name, status, last_update_ts, meta_str))
            self._conn.commit()
            logger.debug("upsert_component_status: %s -> %s", component_name, status)
        except sqlite3.Error as e:
            logger.error("upsert_component_status failed: %s", e)
            raise

    def get_component_status(self, component_name: str) -> dict | None:
        """
        Fetch one component status row. Returns plain dict; meta_json deserialized if present.
        """
        sql = "SELECT component_name, status, last_update_ts, meta_json FROM component_status WHERE component_name = ?"
        try:
            row = self._conn.execute(sql, (component_name,)).fetchone()
            if row is None:
                return None
            out = dict(row)
            if out.get("meta_json") is not None:
                out["meta"] = self._deserialize_json(out.pop("meta_json"))
            else:
                out.pop("meta_json", None)
            return out
        except sqlite3.Error as e:
            logger.error("get_component_status failed: %s", e)
            raise

    def save_market_snapshot(self, snapshot: dict) -> None:
        """
        Upsert latest market snapshot for the symbol in snapshot.
        Expects keys: symbol, last_price, bid, ask, mark_price, oracle_price,
        exchange_ts, local_ts, status, raw (dict to serialize as JSON).
        """
        symbol = snapshot.get("symbol")
        if not symbol:
            logger.warning("save_market_snapshot: missing symbol, skipping")
            return
        raw_str = self._serialize_json(snapshot.get("raw"))
        now = self._now_ms()
        sql = """
            INSERT INTO latest_market_snapshot (
                symbol, last_price, bid, ask, mark_price, oracle_price,
                exchange_ts, local_ts, status, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                last_price = excluded.last_price,
                bid = excluded.bid,
                ask = excluded.ask,
                mark_price = excluded.mark_price,
                oracle_price = excluded.oracle_price,
                exchange_ts = excluded.exchange_ts,
                local_ts = excluded.local_ts,
                status = excluded.status,
                raw_json = excluded.raw_json,
                updated_at = excluded.updated_at
        """
        try:
            self._conn.execute(
                sql,
                (
                    symbol,
                    snapshot.get("last_price"),
                    snapshot.get("bid"),
                    snapshot.get("ask"),
                    snapshot.get("mark_price"),
                    snapshot.get("oracle_price"),
                    snapshot.get("exchange_ts"),
                    snapshot.get("local_ts"),
                    snapshot.get("status"),
                    raw_str,
                    now,
                ),
            )
            self._conn.commit()
            logger.debug("save_market_snapshot: %s", symbol)
        except sqlite3.Error as e:
            logger.error("save_market_snapshot failed: %s", e)
            raise

    def get_latest_market_snapshot(self, symbol: str) -> dict | None:
        """
        Fetch latest market snapshot for symbol. raw_json deserialized to dict if present.
        """
        sql = """
            SELECT symbol, last_price, bid, ask, mark_price, oracle_price,
                   exchange_ts, local_ts, status, raw_json, updated_at
            FROM latest_market_snapshot WHERE symbol = ?
        """
        try:
            row = self._conn.execute(sql, (symbol,)).fetchone()
            if row is None:
                return None
            out = dict(row)
            raw = self._deserialize_json(out.get("raw_json"))
            out.pop("raw_json", None)
            if raw is not None:
                out["raw"] = raw
            return out
        except sqlite3.Error as e:
            logger.error("get_latest_market_snapshot failed: %s", e)
            raise

    def save_closed_candle(self, candle: dict) -> None:
        """
        Insert one closed candle. Ignores duplicate (symbol, interval, open_time) safely.
        Validates required fields before insert.
        """
        self._validate_candle(candle)
        now = self._now_ms()
        sql = """
            INSERT OR IGNORE INTO closed_candles (
                symbol, interval, open_time, close_time, open, high, low, close,
                tick_count, is_closed, inserted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            cur = self._conn.execute(
                sql,
                (
                    candle["symbol"],
                    candle["interval"],
                    candle["open_time"],
                    candle["close_time"],
                    candle["open"],
                    candle["high"],
                    candle["low"],
                    candle["close"],
                    candle.get("tick_count", 0),
                    1 if candle.get("is_closed", True) else 0,
                    now,
                ),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                logger.debug(
                    "save_closed_candle: duplicate ignored symbol=%s interval=%s open_time=%s",
                    candle["symbol"],
                    candle["interval"],
                    candle["open_time"],
                )
            else:
                logger.debug("save_closed_candle: inserted %s %s %s", candle["symbol"], candle["interval"], candle["open_time"])
        except sqlite3.IntegrityError as e:
            logger.warning("save_closed_candle integrity error: %s", e)
            self._conn.rollback()
            raise
        except sqlite3.Error as e:
            logger.error("save_closed_candle failed: %s", e)
            raise

    def save_closed_candles(self, candles: list[dict]) -> None:
        """
        Batch insert closed candles. Duplicates (symbol, interval, open_time) are ignored.
        """
        if not candles:
            return
        now = self._now_ms()
        sql = """
            INSERT OR IGNORE INTO closed_candles (
                symbol, interval, open_time, close_time, open, high, low, close,
                tick_count, is_closed, inserted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        try:
            rows = []
            for c in candles:
                self._validate_candle(c)
                rows.append(
                    (
                        c["symbol"],
                        c["interval"],
                        c["open_time"],
                        c["close_time"],
                        c["open"],
                        c["high"],
                        c["low"],
                        c["close"],
                        c.get("tick_count", 0),
                        1 if c.get("is_closed", True) else 0,
                        now,
                    )
                )
            self._conn.executemany(sql, rows)
            self._conn.commit()
            inserted = sum(1 for _ in rows)  # executemany doesn't report rowcount per row
            logger.info("save_closed_candles: batch of %d candles", len(candles))
        except sqlite3.Error as e:
            logger.error("save_closed_candles failed: %s", e)
            self._conn.rollback()
            raise

    def get_recent_closed_candles(
        self, symbol: str, interval: str, limit: int = 50
    ) -> list[dict]:
        """
        Return most recent closed candles for symbol/interval, ordered by open_time DESC.
        """
        sql = """
            SELECT id, symbol, interval, open_time, close_time, open, high, low, close,
                   tick_count, is_closed, inserted_at
            FROM closed_candles
            WHERE symbol = ? AND interval = ?
            ORDER BY open_time DESC
            LIMIT ?
        """
        try:
            rows = self._conn.execute(sql, (symbol, interval, limit)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as e:
            logger.error("get_recent_closed_candles failed: %s", e)
            raise

    def save_runner_stats(self, runner_name: str, stats: dict) -> None:
        """
        Upsert runner stats. Known columns: loop_iterations, snapshots_seen,
        ticks_processed, duplicate_ticks_skipped, invalid_snapshots, last_tick_ts.
        Extra keys go into meta_json.
        """
        known = {
            "loop_iterations", "snapshots_seen", "ticks_processed",
            "duplicate_ticks_skipped", "invalid_snapshots", "last_tick_ts",
        }
        meta = {k: v for k, v in stats.items() if k not in known}
        meta_str = self._serialize_json(meta) if meta else None
        now = self._now_ms()
        sql = """
            INSERT INTO runner_stats (
                runner_name, loop_iterations, snapshots_seen, ticks_processed,
                duplicate_ticks_skipped, invalid_snapshots, last_tick_ts,
                updated_at, meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(runner_name) DO UPDATE SET
                loop_iterations = excluded.loop_iterations,
                snapshots_seen = excluded.snapshots_seen,
                ticks_processed = excluded.ticks_processed,
                duplicate_ticks_skipped = excluded.duplicate_ticks_skipped,
                invalid_snapshots = excluded.invalid_snapshots,
                last_tick_ts = excluded.last_tick_ts,
                updated_at = excluded.updated_at,
                meta_json = excluded.meta_json
        """
        try:
            self._conn.execute(
                sql,
                (
                    runner_name,
                    stats.get("loop_iterations", 0),
                    stats.get("snapshots_seen", 0),
                    stats.get("ticks_processed", 0),
                    stats.get("duplicate_ticks_skipped", 0),
                    stats.get("invalid_snapshots", 0),
                    stats.get("last_tick_ts"),
                    now,
                    meta_str,
                ),
            )
            self._conn.commit()
            logger.debug("save_runner_stats: %s", runner_name)
        except sqlite3.Error as e:
            logger.error("save_runner_stats failed: %s", e)
            raise

    def get_runner_stats(self, runner_name: str) -> dict | None:
        """
        Fetch runner stats row. meta_json deserialized if present.
        """
        sql = """
            SELECT runner_name, loop_iterations, snapshots_seen, ticks_processed,
                   duplicate_ticks_skipped, invalid_snapshots, last_tick_ts,
                   updated_at, meta_json
            FROM runner_stats WHERE runner_name = ?
        """
        try:
            row = self._conn.execute(sql, (runner_name,)).fetchone()
            if row is None:
                return None
            out = dict(row)
            meta = self._deserialize_json(out.get("meta_json"))
            out.pop("meta_json", None)
            if meta is not None:
                out.update(meta)
            return out
        except sqlite3.Error as e:
            logger.error("get_runner_stats failed: %s", e)
            raise

    def get_db_info(self) -> dict:
        """
        Return database path and row counts per table for debugging.
        """
        tables = (
            "component_status",
            "latest_market_snapshot",
            "closed_candles",
            "runner_stats",
            "paper_trade_events",
            "paper_trades",
            "paper_daily_summary",
        )
        counts = {}
        try:
            for t in tables:
                row = self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
                counts[t] = row["n"] if row else 0
        except sqlite3.Error as e:
            logger.error("get_db_info failed: %s", e)
            raise
        return {"db_path": self._db_path, "counts": counts}

    # -------------------------------------------------------------------------
    # Private helpers
    # -------------------------------------------------------------------------

    def _create_tables(self) -> None:
        """Create all required tables if they do not exist."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS component_status (
                component_name TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                last_update_ts INTEGER NOT NULL,
                meta_json TEXT
            );

            CREATE TABLE IF NOT EXISTS latest_market_snapshot (
                symbol TEXT PRIMARY KEY,
                last_price REAL,
                bid REAL,
                ask REAL,
                mark_price REAL,
                oracle_price REAL,
                exchange_ts INTEGER,
                local_ts INTEGER,
                status TEXT,
                raw_json TEXT,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS closed_candles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                open_time INTEGER NOT NULL,
                close_time INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                tick_count INTEGER NOT NULL,
                is_closed INTEGER NOT NULL,
                inserted_at INTEGER NOT NULL,
                UNIQUE(symbol, interval, open_time)
            );

            CREATE TABLE IF NOT EXISTS runner_stats (
                runner_name TEXT PRIMARY KEY,
                loop_iterations INTEGER NOT NULL,
                snapshots_seen INTEGER NOT NULL,
                ticks_processed INTEGER NOT NULL,
                duplicate_ticks_skipped INTEGER NOT NULL,
                invalid_snapshots INTEGER NOT NULL,
                last_tick_ts INTEGER,
                updated_at INTEGER NOT NULL,
                meta_json TEXT
            );

            -- ----------------------------------------------------------------
            -- Paper trading audit tables (strategy events + trade lifecycle)
            -- ----------------------------------------------------------------
            CREATE TABLE IF NOT EXISTS paper_trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_time_ms INTEGER NOT NULL,
                event_time_iso TEXT NOT NULL,
                ist_date TEXT NOT NULL,
                side TEXT,
                price REAL,
                range_high REAL,
                range_low REAL,
                range_size REAL,
                stop_loss REAL,
                take_profit REAL,
                sl_count INTEGER,
                tp_hit_flag INTEGER,
                halt_reason TEXT,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(strategy_name, symbol, event_type, event_time_ms, side)
            );

            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                trade_date_ist TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_time_ms INTEGER NOT NULL,
                entry_time_iso TEXT NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL,
                take_profit REAL,
                exit_time_ms INTEGER,
                exit_time_iso TEXT,
                exit_price REAL,
                exit_reason TEXT,
                pnl_abs REAL,
                pnl_pct REAL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(strategy_name, symbol, side, entry_time_ms)
            );

            CREATE TABLE IF NOT EXISTS paper_daily_summary (
                strategy_name TEXT NOT NULL,
                symbol TEXT NOT NULL,
                ist_date TEXT NOT NULL,
                total_trades INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                total_pnl_abs REAL NOT NULL,
                halted INTEGER NOT NULL,
                halt_reason TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (strategy_name, symbol, ist_date)
            );
        """)
        self._conn.commit()
        logger.debug("Tables created/verified")

    def _create_indexes(self) -> None:
        """Create indexes for common queries."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_closed_candles_symbol_interval ON closed_candles(symbol, interval)",
            "CREATE INDEX IF NOT EXISTS idx_closed_candles_open_time ON closed_candles(open_time)",
            "CREATE INDEX IF NOT EXISTS idx_paper_trade_events_strategy_symbol_time ON paper_trade_events(strategy_name, symbol, event_time_ms)",
            "CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy_symbol_status ON paper_trades(strategy_name, symbol, status, entry_time_ms)",
            "CREATE INDEX IF NOT EXISTS idx_paper_trades_trade_date ON paper_trades(strategy_name, symbol, trade_date_ist)",
        ]
        for sql in indexes:
            try:
                self._conn.execute(sql)
            except sqlite3.Error as e:
                logger.warning("Index creation: %s", e)
        self._conn.commit()
        logger.debug("Indexes created/verified")

    @staticmethod
    def _serialize_json(value: dict | list | None) -> str | None:
        """Serialize dict/list to JSON string; None stays None."""
        if value is None:
            return None
        try:
            return json.dumps(value)
        except (TypeError, ValueError) as e:
            logger.warning("JSON serialize failed: %s", e)
            return None

    @staticmethod
    def _deserialize_json(value: str | None) -> dict | list | None:
        """Deserialize JSON string to dict/list; None or empty returns None."""
        if value is None or value == "":
            return None
        try:
            return json.loads(value)
        except (TypeError, ValueError) as e:
            logger.warning("JSON deserialize failed: %s", e)
            return None

    @staticmethod
    def _validate_candle(candle: dict) -> None:
        """Raise ValueError if required candle fields are missing."""
        required = ("symbol", "interval", "open_time", "close_time", "open", "high", "low", "close")
        missing = [k for k in required if k not in candle]
        if missing:
            raise ValueError(f"candle missing required fields: {missing}")

    @staticmethod
    def _now_ms() -> int:
        """Current time in milliseconds since epoch."""
        return int(time.time() * 1000)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

    # Use a dedicated demo DB so "before" shows zero counts on every run
    demo_path = "data/bot_state_demo.db"
    store = StateStore(demo_path)
    print("DB info (before):", store.get_db_info(), flush=True)

    store.upsert_component_status(
        component_name="ws_marketdata",
        status="connected",
        last_update_ts=store._now_ms(),
        meta={"symbol": "BTC-USDC"},
    )
    store.save_market_snapshot({
        "symbol": "BTC-USDC",
        "last_price": 97500.5,
        "bid": 97499.0,
        "ask": 97501.0,
        "mark_price": 97500.25,
        "oracle_price": 97500.2,
        "exchange_ts": store._now_ms(),
        "local_ts": store._now_ms(),
        "status": "ok",
        "raw": {"source": "test"},
    })
    store.save_closed_candle({
        "symbol": "BTC-USDC",
        "interval": "1m",
        "open_time": (store._now_ms() // 60000 - 1) * 60000,
        "close_time": (store._now_ms() // 60000) * 60000,
        "open": 97400.0,
        "high": 97550.0,
        "low": 97380.0,
        "close": 97500.5,
        "tick_count": 42,
        "is_closed": True,
    })
    store.save_runner_stats("main_readonly", {
        "loop_iterations": 100,
        "snapshots_seen": 98,
        "ticks_processed": 500,
        "duplicate_ticks_skipped": 10,
        "invalid_snapshots": 0,
        "last_tick_ts": store._now_ms(),
    })

    print("get_component_status:", store.get_component_status("ws_marketdata"), flush=True)
    print("get_latest_market_snapshot:", store.get_latest_market_snapshot("BTC-USDC"), flush=True)
    print("get_recent_closed_candles:", store.get_recent_closed_candles("BTC-USDC", "1m", limit=5), flush=True)
    print("get_runner_stats:", store.get_runner_stats("main_readonly"), flush=True)

    print("DB info (after):", store.get_db_info(), flush=True)
    store.close()
    if Path(demo_path).exists():
        Path(demo_path).unlink()
        print("Removed demo DB.", flush=True)
    print("Done.", flush=True)
