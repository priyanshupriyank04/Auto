"""
SQLite-backed paper trade / event logger for strategy auditability.

This module does NOT place real orders. It only logs:
  - strategy events (paper_trade_events)
  - trade lifecycle (paper_trades)
  - per-day summary (paper_daily_summary)

Design goals:
  - restart-safe / idempotent inserts (dedupe via UNIQUE constraints)
  - consistent IST date handling (Asia/Kolkata)
  - minimal coupling to runners/strategies: accepts raw strategy event dicts and
    normalizes them into a small set of auditable event types.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.state_store import StateStore

IST = ZoneInfo("Asia/Kolkata")


def _utc_ms_to_dt(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)


def _utc_ms_to_iso_z(timestamp_ms: int) -> str:
    # Stable ISO string in UTC with Z suffix (no microseconds).
    return _utc_ms_to_dt(timestamp_ms).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_ms_to_ist_date(timestamp_ms: int) -> str:
    return _utc_ms_to_dt(timestamp_ms).astimezone(IST).strftime("%Y-%m-%d")


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _now_iso_utc() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def _serialize_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class NormalizedEvent:
    strategy_name: str
    symbol: str
    event_type: str
    event_time_ms: int
    side: str | None = None
    price: float | None = None
    range_high: float | None = None
    range_low: float | None = None
    range_size: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    sl_count: int | None = None
    tp_hit_flag: int | None = None
    halt_reason: str | None = None
    payload: dict[str, Any] | None = None


class PaperTradeLogger:
    """
    SQLite-backed paper event and trade logger.

    The logger is restart-safe:
      - Events dedupe via UNIQUE(strategy_name, symbol, event_type, event_time_ms, side)
      - Trades dedupe via UNIQUE(strategy_name, symbol, side, entry_time_ms)
      - Closing a trade is idempotent (only closes OPEN trade; ignores if already closed)
      - Daily summary is upserted deterministically from DB state
    """

    def __init__(
        self,
        store: StateStore,
        strategy_name: str = "breakout_strategy",
        symbol: str = "BTC-USDC",
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._conn: sqlite3.Connection = store.get_connection()
        self._strategy_name = strategy_name
        self._symbol = symbol
        self._log = logger or logging.getLogger("paper_trade_logger")
        self._lock = threading.RLock()

    # ---------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------
    def log_strategy_event(self, event: dict[str, Any]) -> list[int]:
        """
        Accept raw strategy result dict(s), normalize into 0..N NormalizedEvent(s),
        persist into paper_trade_events, and manage trade lifecycle + daily summary.

        Returns list of inserted event row ids (may be empty if deduped).
        """
        normalized = self._normalize_raw_event(event)
        inserted_ids: list[int] = []
        for ne in normalized:
            row_id = self._insert_event(ne)
            if row_id is not None:
                inserted_ids.append(row_id)
            # Trade lifecycle hooks are safe to call even if event was deduped.
            if ne.event_type in ("entry_long", "entry_short"):
                self.open_trade_from_event(ne)
            elif ne.event_type in ("tp_hit", "sl_hit"):
                self.close_trade_from_event(ne)
            elif ne.event_type == "trading_halted":
                self.update_daily_summary(ist_date=_utc_ms_to_ist_date(ne.event_time_ms), halt_reason=ne.halt_reason)
        # Keep daily summary fresh after lifecycle changes.
        if normalized:
            self.update_daily_summary(ist_date=_utc_ms_to_ist_date(normalized[-1].event_time_ms))
        return inserted_ids

    def open_trade_from_event(self, event: NormalizedEvent) -> int | None:
        """Create an OPEN trade from an entry event. Idempotent by UNIQUE constraint."""
        if event.event_type not in ("entry_long", "entry_short"):
            return None
        if event.price is None:
            return None
        side = "long" if event.event_type == "entry_long" else "short"
        entry_time_ms = event.event_time_ms
        trade_date_ist = _utc_ms_to_ist_date(entry_time_ms)
        now_iso = _now_iso_utc()

        with self._lock:
            sql = """
                INSERT OR IGNORE INTO paper_trades (
                    strategy_name, symbol, trade_date_ist, side,
                    entry_time_ms, entry_time_iso, entry_price,
                    stop_loss, take_profit,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cur = self._conn.execute(
                sql,
                (
                    event.strategy_name,
                    event.symbol,
                    trade_date_ist,
                    side,
                    entry_time_ms,
                    _utc_ms_to_iso_z(entry_time_ms),
                    float(event.price),
                    event.stop_loss,
                    event.take_profit,
                    "OPEN",
                    now_iso,
                    now_iso,
                ),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)

    def close_trade_from_event(self, event: NormalizedEvent) -> int | None:
        """
        Close the latest OPEN trade for this strategy+symbol.
        Idempotent: if there's no OPEN trade, does nothing.
        """
        if event.event_type not in ("tp_hit", "sl_hit"):
            return None
        if event.price is None:
            return None

        with self._lock:
            open_trade = self.get_open_trade(strategy_name=event.strategy_name, symbol=event.symbol)
            if not open_trade:
                return None

            trade_id = int(open_trade["id"])
            side = (open_trade.get("side") or "").lower()
            entry_price = float(open_trade["entry_price"])
            exit_price = float(event.price)
            exit_time_ms = event.event_time_ms

            if side == "long":
                pnl_abs = exit_price - entry_price
            else:
                pnl_abs = entry_price - exit_price
            pnl_pct = (pnl_abs / entry_price) * 100.0 if entry_price else None

            if event.event_type == "tp_hit":
                status = "CLOSED_TP"
                exit_reason = "tp"
            else:
                status = "CLOSED_SL"
                exit_reason = "sl"

            now_iso = _now_iso_utc()
            sql = """
                UPDATE paper_trades
                SET exit_time_ms = ?,
                    exit_time_iso = ?,
                    exit_price = ?,
                    exit_reason = ?,
                    pnl_abs = ?,
                    pnl_pct = ?,
                    status = ?,
                    updated_at = ?
                WHERE id = ? AND status = 'OPEN'
            """
            cur = self._conn.execute(
                sql,
                (
                    exit_time_ms,
                    _utc_ms_to_iso_z(exit_time_ms),
                    exit_price,
                    exit_reason,
                    pnl_abs,
                    pnl_pct,
                    status,
                    now_iso,
                    trade_id,
                ),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return trade_id

    def update_daily_summary(self, ist_date: str, halt_reason: str | None = None) -> None:
        """
        Upsert deterministic daily summary for (strategy_name, symbol, ist_date).
        Summary is computed from paper_trades + latest halted event.
        """
        strategy_name = self._strategy_name
        symbol = self._symbol
        now_iso = _now_iso_utc()

        with self._lock:
            # Compute totals from trades for the day.
            sql_trades = """
                SELECT
                    COUNT(*) AS total_trades,
                    SUM(CASE WHEN pnl_abs > 0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_abs < 0 THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(COALESCE(pnl_abs, 0)), 0) AS total_pnl_abs
                FROM paper_trades
                WHERE strategy_name = ? AND symbol = ? AND trade_date_ist = ? AND status != 'OPEN'
            """
            row = self._conn.execute(sql_trades, (strategy_name, symbol, ist_date)).fetchone()
            total_trades = int(row["total_trades"] or 0) if row else 0
            wins = int(row["wins"] or 0) if row else 0
            losses = int(row["losses"] or 0) if row else 0
            total_pnl_abs = float(row["total_pnl_abs"] or 0.0) if row else 0.0

            # Determine halted flag/reason: use most recent trading_halted event for the day if present.
            sql_halt = """
                SELECT halt_reason
                FROM paper_trade_events
                WHERE strategy_name = ? AND symbol = ? AND ist_date = ? AND event_type = 'trading_halted'
                ORDER BY event_time_ms DESC
                LIMIT 1
            """
            halt_row = self._conn.execute(sql_halt, (strategy_name, symbol, ist_date)).fetchone()
            halted = 1 if halt_row else 0
            final_halt_reason = (halt_row["halt_reason"] if halt_row and halt_row["halt_reason"] else None) or halt_reason

            sql_upsert = """
                INSERT INTO paper_daily_summary (
                    strategy_name, symbol, ist_date,
                    total_trades, wins, losses, total_pnl_abs,
                    halted, halt_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_name, symbol, ist_date) DO UPDATE SET
                    total_trades = excluded.total_trades,
                    wins = excluded.wins,
                    losses = excluded.losses,
                    total_pnl_abs = excluded.total_pnl_abs,
                    halted = excluded.halted,
                    halt_reason = excluded.halt_reason,
                    updated_at = excluded.updated_at
            """
            self._conn.execute(
                sql_upsert,
                (
                    strategy_name,
                    symbol,
                    ist_date,
                    total_trades,
                    wins,
                    losses,
                    total_pnl_abs,
                    halted,
                    final_halt_reason,
                    now_iso,
                ),
            )
            self._conn.commit()

    def get_open_trade(self, strategy_name: str | None = None, symbol: str | None = None) -> dict[str, Any] | None:
        """Return the latest OPEN trade for strategy+symbol, or None."""
        s = strategy_name or self._strategy_name
        sym = symbol or self._symbol
        with self._lock:
            sql = """
                SELECT *
                FROM paper_trades
                WHERE strategy_name = ? AND symbol = ? AND status = 'OPEN'
                ORDER BY entry_time_ms DESC
                LIMIT 1
            """
            row = self._conn.execute(sql, (s, sym)).fetchone()
            return dict(row) if row else None

    def get_today_summary(self, ist_date: str | None = None) -> dict[str, Any] | None:
        """Return daily summary row for today (IST) or a provided IST date."""
        d = ist_date or _utc_ms_to_ist_date(_now_ms())
        with self._lock:
            sql = """
                SELECT *
                FROM paper_daily_summary
                WHERE strategy_name = ? AND symbol = ? AND ist_date = ?
            """
            row = self._conn.execute(sql, (self._strategy_name, self._symbol, d)).fetchone()
            if row:
                return dict(row)
        # If missing, compute and upsert, then read back.
        self.update_daily_summary(ist_date=d)
        with self._lock:
            row2 = self._conn.execute(sql, (self._strategy_name, self._symbol, d)).fetchone()
            return dict(row2) if row2 else None

    def get_recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return most recent paper_trade_events rows for strategy+symbol."""
        limit = max(1, min(int(limit), 500))
        with self._lock:
            sql = """
                SELECT *
                FROM paper_trade_events
                WHERE strategy_name = ? AND symbol = ?
                ORDER BY event_time_ms DESC, id DESC
                LIMIT ?
            """
            rows = self._conn.execute(sql, (self._strategy_name, self._symbol, limit)).fetchall()
            return [dict(r) for r in rows]

    # ---------------------------------------------------------------------
    # Internal: normalization + persistence
    # ---------------------------------------------------------------------
    def _insert_event(self, e: NormalizedEvent) -> int | None:
        """Insert event row (deduped). Return inserted row id or None."""
        with self._lock:
            sql = """
                INSERT OR IGNORE INTO paper_trade_events (
                    strategy_name, symbol, event_type, event_time_ms, event_time_iso, ist_date,
                    side, price, range_high, range_low, range_size, stop_loss, take_profit,
                    sl_count, tp_hit_flag, halt_reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cur = self._conn.execute(
                sql,
                (
                    e.strategy_name,
                    e.symbol,
                    e.event_type,
                    e.event_time_ms,
                    _utc_ms_to_iso_z(e.event_time_ms),
                    _utc_ms_to_ist_date(e.event_time_ms),
                    e.side,
                    e.price,
                    e.range_high,
                    e.range_low,
                    e.range_size,
                    e.stop_loss,
                    e.take_profit,
                    e.sl_count,
                    e.tp_hit_flag,
                    e.halt_reason,
                    _serialize_json(e.payload),
                    _now_iso_utc(),
                ),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid)

    def _normalize_raw_event(self, raw: dict[str, Any]) -> list[NormalizedEvent]:
        """
        Convert the strategy engine's returned dict into our normalized event set.

        Supports breakout_strategy.py output:
          - closed candle processing: {"pair_found": True, "range_high":..., ...}
          - live price processing: {"entry": {"signal": "long_entry"|"short_entry", ...}}
                               and {"exit": {"exit": "tp"|"sl", "side": "long"|"short", "price": ...}}
          - {"new_day_reset": True, "strategy_date_ist": "YYYY-MM-DD"}
          - {"halt_or_session_ended": True} or exit_evt {"halted": True}
        """
        if not raw or not isinstance(raw, dict):
            return []

        out: list[NormalizedEvent] = []
        strategy_name = self._strategy_name
        symbol = self._symbol

        # Determine a best event_time_ms for this raw event.
        # Prefer: candle open_time (closed candle events), else last_live_ts_ms (if present), else now.
        event_time_ms = (
            _safe_int(raw.get("open_time"))
            or _safe_int(raw.get("timestamp_ms"))
            or _safe_int(raw.get("ts_ms"))
            or _safe_int(raw.get("event_time_ms"))
            or _now_ms()
        )

        # 1) new day reset
        if raw.get("new_day_reset"):
            out.append(
                NormalizedEvent(
                    strategy_name=strategy_name,
                    symbol=symbol,
                    event_type="new_day_reset",
                    event_time_ms=event_time_ms,
                    payload={"strategy_date_ist": raw.get("strategy_date_ist")},
                )
            )

        # 2) pair found (range defined)
        if raw.get("pair_found"):
            out.append(
                NormalizedEvent(
                    strategy_name=strategy_name,
                    symbol=symbol,
                    event_type="pair_found",
                    event_time_ms=event_time_ms,
                    range_high=_safe_float(raw.get("range_high")),
                    range_low=_safe_float(raw.get("range_low")),
                    range_size=_safe_float(raw.get("range_size")),
                    payload=raw,
                )
            )

        # 3) entry events
        entry = raw.get("entry")
        if isinstance(entry, dict):
            sig = entry.get("signal")
            price = _safe_float(entry.get("price"))
            sl = _safe_float(entry.get("sl"))
            tp = _safe_float(entry.get("tp"))
            if sig == "long_entry":
                out.append(
                    NormalizedEvent(
                        strategy_name=strategy_name,
                        symbol=symbol,
                        event_type="entry_long",
                        event_time_ms=event_time_ms,
                        side="long",
                        price=price,
                        stop_loss=sl,
                        take_profit=tp,
                        payload=raw,
                    )
                )
            elif sig == "short_entry":
                out.append(
                    NormalizedEvent(
                        strategy_name=strategy_name,
                        symbol=symbol,
                        event_type="entry_short",
                        event_time_ms=event_time_ms,
                        side="short",
                        price=price,
                        stop_loss=sl,
                        take_profit=tp,
                        payload=raw,
                    )
                )

        # 4) exit events (tp/sl)
        exit_evt = raw.get("exit")
        if isinstance(exit_evt, dict):
            exit_type = (exit_evt.get("exit") or "").lower()
            side = (exit_evt.get("side") or "").lower() or None
            price = _safe_float(exit_evt.get("price"))
            sl_count = _safe_int(exit_evt.get("sl_count"))
            halted = bool(exit_evt.get("halted"))
            if exit_type == "tp":
                out.append(
                    NormalizedEvent(
                        strategy_name=strategy_name,
                        symbol=symbol,
                        event_type="tp_hit",
                        event_time_ms=event_time_ms,
                        side=side,
                        price=price,
                        tp_hit_flag=1,
                        payload=raw,
                    )
                )
                if halted:
                    out.append(
                        NormalizedEvent(
                            strategy_name=strategy_name,
                            symbol=symbol,
                            event_type="trading_halted",
                            event_time_ms=event_time_ms,
                            halt_reason="tp_hit",
                            payload=raw,
                        )
                    )
            elif exit_type == "sl":
                out.append(
                    NormalizedEvent(
                        strategy_name=strategy_name,
                        symbol=symbol,
                        event_type="sl_hit",
                        event_time_ms=event_time_ms,
                        side=side,
                        price=price,
                        sl_count=sl_count,
                        payload=raw,
                    )
                )
                if halted:
                    out.append(
                        NormalizedEvent(
                            strategy_name=strategy_name,
                            symbol=symbol,
                            event_type="trading_halted",
                            event_time_ms=event_time_ms,
                            halt_reason="3_sl_hits",
                            payload=raw,
                        )
                    )

        # 5) explicit halt / session end (from strategy result)
        # breakout_strategy sets "halt_or_session_ended": True when halted or session ended.
        if raw.get("halt_or_session_ended") and not any(e.event_type == "trading_halted" for e in out):
            out.append(
                NormalizedEvent(
                    strategy_name=strategy_name,
                    symbol=symbol,
                    event_type="trading_halted",
                    event_time_ms=event_time_ms,
                    halt_reason="halt_or_session_ended",
                    payload=raw,
                )
            )

        return out

