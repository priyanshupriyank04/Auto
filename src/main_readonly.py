r"""
Read-only integration runner for Hyperliquid market data + candle building.

This module provides:
  - ReadOnlyRunner: starts WebSocket market data, polls latest snapshot,
    extracts ticks, feeds CandleBuilder, and logs heartbeats.
  - No trading, no order placement, no database, no risk controller.
  - Clean shutdown on KeyboardInterrupt.

Use this file only for running the system in read-only integration mode.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.candle_builder import CandleBuilder
from src.state_store import StateStore
from src.ws_marketdata import HyperliquidWSMarketData

# Component names for persistence
RUNNER_COMPONENT = "main_readonly"
WS_COMPONENT = "ws_marketdata"


def _setup_logging() -> logging.Logger:
    """Configure and return the module logger."""
    log = logging.getLogger("main_readonly")
    if not log.handlers:
        log.setLevel(logging.DEBUG)
        h = logging.StreamHandler()
        h.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s %(funcName)s: %(message)s"
        )
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


def _safe_float(value: Any) -> float | None:
    """Parse float from string or number; return None on failure."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


def _normalize_ts_ms(ts: Any) -> int | None:
    """Normalize timestamp to milliseconds. Accepts int/float in sec or ms."""
    if ts is None:
        return None
    try:
        t = int(float(ts))
        if t < 0 or t > 2**62:
            return None
        # If value looks like seconds (e.g. 10 digits), convert to ms
        if t < 1e12:
            t = t * 1000
        return t
    except (ValueError, TypeError):
        return None


def _now_ms() -> int:
    """Current time in milliseconds since epoch."""
    return int(time.time() * 1000)


class ReadOnlyRunner:
    """
    Read-only runner: WebSocket market data -> snapshot polling -> tick extraction
    -> CandleBuilder. Persists snapshots, closed candles, runner stats, and
    component status via StateStore. Logs heartbeats; no trading or order logic.
    """

    def __init__(
        self,
        symbol: str = "BTC-USDC",
        poll_interval_seconds: float = 0.25,
        heartbeat_interval_seconds: float = 5.0,
    ) -> None:
        """
        Initialize the read-only runner.

        Args:
            symbol: Canonical symbol (e.g. BTC-USDC).
            poll_interval_seconds: Sleep between main loop iterations.
            heartbeat_interval_seconds: Interval between heartbeat logs.
        """
        self.symbol = symbol
        self.poll_interval_seconds = poll_interval_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._logger = _setup_logging()

        self._ws = HyperliquidWSMarketData(symbol=symbol)
        self._candle_builder = CandleBuilder(symbol=symbol)
        self.state_store = StateStore()

        self._running = False
        self._last_tick_price: float | None = None
        self._last_tick_ts_ms: int | None = None

        self.loop_iterations = 0
        self.snapshots_seen = 0
        self.ticks_processed = 0
        self.duplicate_ticks_skipped = 0
        self.invalid_snapshots = 0
        self.start_time: float | None = None
        self.last_tick_ts: int | None = None

        self.snapshots_persisted = 0
        self.candles_persisted = 0
        self.status_updates = 0

        self._last_heartbeat_time: float = 0.0

    def start(self) -> None:
        """
        Start WebSocket market data and enter the main processing loop.
        Runs until stop() is called or KeyboardInterrupt.
        """
        self._logger.info(
            "start symbol=%s poll_interval=%.2fs heartbeat_interval=%.2fs",
            self.symbol,
            self.poll_interval_seconds,
            self.heartbeat_interval_seconds,
        )
        self.start_time = time.time()
        self._last_heartbeat_time = time.time()
        self._running = True

        self._update_component_status(RUNNER_COMPONENT, "starting")
        self._update_component_status(WS_COMPONENT, "starting")
        self._ws.start()
        self._update_component_status(RUNNER_COMPONENT, "running")
        self._update_component_status(WS_COMPONENT, "running")
        self._logger.info("websocket started for %s; persistence active", self.symbol)
        try:
            self._run_loop()
        finally:
            self._shutdown_persistence()
            self.stop()
            self._log_final_summary()

    def stop(self) -> None:
        """Stop the loop and disconnect WebSocket. Call from any thread; DB shutdown runs in main thread."""
        self._logger.info("stop requested")
        self._running = False
        self._ws.stop()

    def _shutdown_persistence(self) -> None:
        """Update component status, save final runner stats, close DB. Must be called from main thread only."""
        try:
            self._update_component_status(RUNNER_COMPONENT, "stopping")
            self._persist_runner_stats()
            self._update_component_status(RUNNER_COMPONENT, "stopped")
            self._update_component_status(WS_COMPONENT, "stopped")
        except Exception as e:
            self._logger.warning("persistence during shutdown: %s", e)
        try:
            self.state_store.close()
        except Exception as e:
            self._logger.warning("state_store.close: %s", e)

    def _update_component_status(
        self, component_name: str, status: str, meta: dict[str, Any] | None = None
    ) -> None:
        """Upsert component status in DB. Logs errors but does not raise."""
        try:
            self.state_store.upsert_component_status(
                component_name=component_name,
                status=status,
                last_update_ts=_now_ms(),
                meta=meta,
            )
            self.status_updates += 1
        except Exception as e:
            self._logger.warning("persist component_status failed %s=%s: %s", component_name, status, e)

    def _update_component_statuses(self) -> None:
        """Update runner and websocket component status with current state (for heartbeat)."""
        status = self._ws.get_connection_status()
        fresh = self._ws.is_data_fresh(max_age_seconds=2.0)
        ws_status = "running" if (status.get("connected") and fresh) else (
            "degraded" if status.get("connected") else "disconnected"
        )
        self._update_component_status(
            RUNNER_COMPONENT,
            "running",
            meta={"loop_iterations": self.loop_iterations, "last_tick_ts": self.last_tick_ts},
        )
        self._update_component_status(
            WS_COMPONENT,
            ws_status,
            meta={
                "connected": status.get("connected"),
                "data_fresh": fresh,
                "messages_received": status.get("messages_received"),
            },
        )

    def _persist_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Persist latest market snapshot. Logs errors but does not raise."""
        if not isinstance(snapshot, dict) or not snapshot.get("symbol"):
            return
        try:
            self.state_store.save_market_snapshot(snapshot)
            self.snapshots_persisted += 1
        except Exception as e:
            self._logger.warning("persist snapshot failed: %s", e)

    def _persist_closed_candles(self, event: dict[str, Any]) -> None:
        """Persist new_1m_closed and new_5m_closed from process_tick result. Logs errors but does not raise."""
        for key, interval in (("new_1m_closed", "1m"), ("new_5m_closed", "5m")):
            candle = event.get(key)
            if not candle or not isinstance(candle, dict):
                continue
            try:
                self.state_store.save_closed_candle(candle)
                self.candles_persisted += 1
                self._logger.info("persisted closed candle %s open_time=%s", interval, candle.get("open_time"))
            except Exception as e:
                self._logger.warning("persist closed_candle %s failed: %s", interval, e)

    def _persist_runner_stats(self) -> None:
        """Persist runner stats to DB. Extra fields (e.g. persistence counters) go in meta_json."""
        stats_dict: dict[str, Any] = {
            "loop_iterations": self.loop_iterations,
            "snapshots_seen": self.snapshots_seen,
            "ticks_processed": self.ticks_processed,
            "duplicate_ticks_skipped": self.duplicate_ticks_skipped,
            "invalid_snapshots": self.invalid_snapshots,
            "last_tick_ts": self.last_tick_ts,
            "snapshots_persisted": self.snapshots_persisted,
            "candles_persisted": self.candles_persisted,
            "status_updates": self.status_updates,
        }
        try:
            self.state_store.save_runner_stats(RUNNER_COMPONENT, stats_dict)
        except Exception as e:
            self._logger.warning("persist runner_stats failed: %s", e)

    def _run_loop(self) -> None:
        """Main loop: fetch snapshot -> process -> heartbeat -> sleep."""
        while self._running:
            self.loop_iterations += 1
            try:
                snapshot = self._ws.get_latest_snapshot()
                self.snapshots_seen += 1
                self._process_snapshot(snapshot)

                now = time.time()
                if now - self._last_heartbeat_time >= self.heartbeat_interval_seconds:
                    self._last_heartbeat_time = now
                    self._log_heartbeat()
            except Exception as e:
                self._logger.exception("run_loop_error: %s", e)
            time.sleep(self.poll_interval_seconds)

    def _extract_tick_from_snapshot(self, snapshot: dict[str, Any]) -> tuple[float, int] | None:
        """
        Extract (price, timestamp_ms) from snapshot.
        Primary price: last_price; fallback: midpoint of bid/ask.
        Primary ts: exchange_ts; fallback: local_ts.
        Returns None if no usable price or timestamp.
        """
        if not isinstance(snapshot, dict):
            return None

        price: float | None = None
        raw_last = snapshot.get("last_price")
        price = _safe_float(raw_last)
        if price is None:
            bid = _safe_float(snapshot.get("bid"))
            ask = _safe_float(snapshot.get("ask"))
            if bid is not None and ask is not None:
                price = (bid + ask) / 2.0
        if price is None or price <= 0:
            return None

        ts_ms = _normalize_ts_ms(snapshot.get("exchange_ts"))
        if ts_ms is None:
            ts_ms = _normalize_ts_ms(snapshot.get("local_ts"))
        if ts_ms is None:
            return None

        return (price, ts_ms)

    def _should_process_tick(self, price: float, timestamp_ms: int) -> bool:
        """Return True if this tick is new (not a duplicate of last processed)."""
        if self._last_tick_price is None and self._last_tick_ts_ms is None:
            return True
        if self._last_tick_ts_ms == timestamp_ms and self._last_tick_price == price:
            return False
        return True

    def _process_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Extract tick, deduplicate, feed CandleBuilder, persist snapshot/candles, update stats, log new closed candles."""
        if isinstance(snapshot, dict) and snapshot.get("symbol"):
            self._persist_snapshot(snapshot)
        tick = self._extract_tick_from_snapshot(snapshot)
        if tick is None:
            self.invalid_snapshots += 1
            if self.snapshots_seen <= 3 or self.invalid_snapshots % 100 == 1:
                self._logger.debug(
                    "invalid_snapshot snapshots_seen=%s (no usable price/ts)",
                    self.snapshots_seen,
                )
            return

        price, timestamp_ms = tick
        if not self._should_process_tick(price, timestamp_ms):
            self.duplicate_ticks_skipped += 1
            return

        try:
            result = self._candle_builder.process_tick(price, timestamp_ms)
        except Exception as e:
            self._logger.warning("candle_builder process_tick error: %s", e)
            return

        self._last_tick_price = price
        self._last_tick_ts_ms = timestamp_ms
        self.ticks_processed += 1
        self.last_tick_ts = timestamp_ms

        self._persist_closed_candles(result)
        if result.get("new_1m_closed"):
            summary = self._summarize_candle(result["new_1m_closed"])
            self._logger.info("closed_1m %s", summary)
        if result.get("new_5m_closed"):
            summary = self._summarize_candle(result["new_5m_closed"])
            self._logger.info("closed_5m %s", summary)

    def _summarize_candle(self, candle: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return a compact log-friendly dict for a candle."""
        if candle is None:
            return None
        return {
            "open_time": candle.get("open_time"),
            "close_time": candle.get("close_time"),
            "o": candle.get("open"),
            "h": candle.get("high"),
            "l": candle.get("low"),
            "c": candle.get("close"),
            "tick_count": candle.get("tick_count"),
        }

    def _log_heartbeat(self) -> None:
        """Log a readable status summary; persist runner stats and component statuses."""
        self._persist_runner_stats()
        self._update_component_statuses()

        status = self._ws.get_connection_status()
        snapshot = self._ws.get_latest_snapshot()
        fresh = self._ws.is_data_fresh(max_age_seconds=2.0)

        current_1m = self._candle_builder.get_current_1m()
        current_5m = self._candle_builder.get_current_5m()
        latest_closed_1m = self._candle_builder.get_latest_closed_1m()
        latest_closed_5m = self._candle_builder.get_latest_closed_5m()
        builder_stats = self._candle_builder.get_stats()

        uptime = (time.time() - self.start_time) if self.start_time else 0

        self._logger.info(
            "heartbeat ws_connected=%s data_fresh=%s status=%s uptime_sec=%.1f "
            "iterations=%s snapshots=%s ticks=%s duplicates_skipped=%s invalid_snapshots=%s last_tick_ts=%s "
            "persisted: snapshots=%s candles=%s status_updates=%s "
            "cb_ticks=%s cb_1m_closed=%s cb_5m_closed=%s",
            status.get("connected"),
            fresh,
            status.get("status"),
            uptime,
            self.loop_iterations,
            self.snapshots_seen,
            self.ticks_processed,
            self.duplicate_ticks_skipped,
            self.invalid_snapshots,
            self.last_tick_ts,
            self.snapshots_persisted,
            self.candles_persisted,
            self.status_updates,
            builder_stats.get("total_ticks_processed"),
            builder_stats.get("total_1m_closed"),
            builder_stats.get("total_5m_closed"),
        )
        self._logger.info(
            "heartbeat snapshot last_price=%s bid=%s ask=%s exchange_ts=%s local_ts=%s",
            snapshot.get("last_price"),
            snapshot.get("bid"),
            snapshot.get("ask"),
            snapshot.get("exchange_ts"),
            snapshot.get("local_ts"),
        )
        self._logger.info(
            "heartbeat current_1m=%s current_5m=%s",
            self._summarize_candle(current_1m),
            self._summarize_candle(current_5m),
        )
        self._logger.info(
            "heartbeat latest_closed_1m=%s latest_closed_5m=%s",
            self._summarize_candle(latest_closed_1m),
            self._summarize_candle(latest_closed_5m),
        )

    def _log_final_summary(self) -> None:
        """Log final runner, persistence, and builder stats on shutdown."""
        uptime = (time.time() - self.start_time) if self.start_time else 0
        self._logger.info(
            "shutdown complete uptime_sec=%.1f loop_iterations=%s snapshots_seen=%s "
            "ticks_processed=%s duplicate_ticks_skipped=%s invalid_snapshots=%s "
            "snapshots_persisted=%s candles_persisted=%s status_updates=%s",
            uptime,
            self.loop_iterations,
            self.snapshots_seen,
            self.ticks_processed,
            self.duplicate_ticks_skipped,
            self.invalid_snapshots,
            self.snapshots_persisted,
            self.candles_persisted,
            self.status_updates,
        )
        stats = self._candle_builder.get_stats()
        self._logger.info("candle_builder final stats: %s", stats)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    runner = ReadOnlyRunner(symbol="BTC-USDC")
    try:
        runner.start()
    except KeyboardInterrupt:
        runner.stop()
        print("Read-only runner stopped (KeyboardInterrupt). Shutdown complete.")
