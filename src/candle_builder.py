r"""
In-memory candle builder for Hyperliquid (1m and 5m OHLC).

This module provides:
  - CandleBuilder: accepts live tick/price updates, builds and maintains
    1-minute and 5-minute OHLC candles in memory
  - Optional bootstrap from historical REST candles
  - Thread-safe access; suitable for consumption by strategy logic

This file is NOT for:
  - Strategy logic
  - Order placement
  - WebSocket connection
  - Database or persistent storage
"""

from __future__ import annotations

import copy
import logging
import threading
from collections import deque
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MS_PER_MINUTE = 60_000
MS_PER_5MIN = 300_000

REQUIRED_CANDLE_KEYS = frozenset({
    "symbol", "interval", "open_time", "close_time",
    "open", "high", "low", "close", "tick_count", "is_closed",
})

logger = logging.getLogger(__name__)


def _default_candle(
    symbol: str,
    interval: str,
    open_time: int,
    close_time: int,
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    tick_count: int,
    is_closed: bool,
) -> dict[str, Any]:
    """Build a normalized candle dict (all numeric prices as float)."""
    return {
        "symbol": symbol,
        "interval": interval,
        "open_time": open_time,
        "close_time": close_time,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "tick_count": tick_count,
        "is_closed": is_closed,
    }


class CandleBuilder:
    """
    Builds and maintains 1m and 5m OHLC candles in memory from live ticks.
    Thread-safe; supports optional bootstrap from historical candles.
    """

    def __init__(
        self,
        symbol: str = "BTC-USDC",
        max_closed_1m: int = 500,
        max_closed_5m: int = 500,
    ) -> None:
        """
        Initialize the candle builder.

        Args:
            symbol: Canonical symbol (e.g. BTC-USDC).
            max_closed_1m: Max number of closed 1m candles to keep in memory.
            max_closed_5m: Max number of closed 5m candles to keep in memory.
        """
        self._symbol = symbol
        self._lock = threading.RLock()
        self._current_1m: dict[str, Any] | None = None
        self._current_5m: dict[str, Any] | None = None
        self._closed_1m: deque[dict[str, Any]] = deque(maxlen=max_closed_1m)
        self._closed_5m: deque[dict[str, Any]] = deque(maxlen=max_closed_5m)
        self._total_ticks_processed = 0
        self._total_1m_closed = 0
        self._total_5m_closed = 0
        self._last_processed_ts: int | None = None
        self._backward_ts_count = 0

    # -------------------------------------------------------------------------
    # Bucket alignment (wall-clock: 1m = minute boundaries, 5m = 00,05,10,...)
    # -------------------------------------------------------------------------
    @staticmethod
    def _get_1m_bucket_start(timestamp_ms: int) -> int:
        """Return start of the 1-minute bucket containing timestamp_ms (UTC)."""
        return (timestamp_ms // MS_PER_MINUTE) * MS_PER_MINUTE

    @staticmethod
    def _get_5m_bucket_start(timestamp_ms: int) -> int:
        """Return start of the 5-minute bucket containing timestamp_ms (UTC)."""
        return (timestamp_ms // MS_PER_5MIN) * MS_PER_5MIN

    @staticmethod
    def _bucket_close_time(bucket_start_ms: int, interval: str) -> int:
        """Return close time (exclusive end) of the bucket in ms."""
        if interval == "1m":
            return bucket_start_ms + MS_PER_MINUTE
        if interval == "5m":
            return bucket_start_ms + MS_PER_5MIN
        raise ValueError(f"Unsupported interval: {interval}")

    # -------------------------------------------------------------------------
    # Validation
    # -------------------------------------------------------------------------
    @staticmethod
    def _validate_price(price: float) -> float:
        """Validate and return price; raise ValueError if invalid."""
        if not isinstance(price, (int, float)):
            raise ValueError(f"Invalid price type: {type(price)}")
        p = float(price)
        if not (p > 0 and p < 1e15):
            raise ValueError(f"Invalid price value: {p}")
        return p

    @staticmethod
    def _validate_timestamp(timestamp_ms: int) -> int:
        """Validate and return timestamp_ms; raise ValueError if invalid."""
        if not isinstance(timestamp_ms, (int, float)):
            raise ValueError(f"Invalid timestamp type: {type(timestamp_ms)}")
        t = int(timestamp_ms)
        if t < 0 or t > 2**62:
            raise ValueError(f"Invalid timestamp: {t}")
        return t

    # -------------------------------------------------------------------------
    # Candle lifecycle (internal)
    # -------------------------------------------------------------------------
    def _new_candle(self, interval: str, bucket_start_ms: int, price: float) -> dict[str, Any]:
        """Create a new active candle for the given bucket and first price."""
        close_time = self._bucket_close_time(bucket_start_ms, interval)
        candle = _default_candle(
            symbol=self._symbol,
            interval=interval,
            open_time=bucket_start_ms,
            close_time=close_time,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            tick_count=1,
            is_closed=False,
        )
        logger.debug(
            "New %s candle created bucket_start=%s",
            interval,
            bucket_start_ms,
        )
        return candle

    @staticmethod
    def _update_candle(candle: dict[str, Any], price: float) -> None:
        """Update candle in place with new price (high, low, close, tick_count)."""
        candle["high"] = max(candle["high"], price)
        candle["low"] = min(candle["low"], price)
        candle["close"] = price
        candle["tick_count"] = candle.get("tick_count", 0) + 1

    @staticmethod
    def _finalize_candle(candle: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of the candle marked as closed (do not mutate original)."""
        out = copy.deepcopy(candle)
        out["is_closed"] = True
        return out

    @staticmethod
    def _copy_candle(candle: dict[str, Any] | None) -> dict[str, Any] | None:
        """Return a deep copy of the candle or None."""
        if candle is None:
            return None
        return copy.deepcopy(candle)

    def _normalize_bootstrap_candle(self, raw: dict[str, Any], interval: str) -> dict[str, Any]:
        """Convert external candle dict to our normalized format (closed)."""
        open_time = int(raw.get("open_time", 0))
        if interval == "1m":
            bucket_start = self._get_1m_bucket_start(open_time)
        else:
            bucket_start = self._get_5m_bucket_start(open_time)
        close_time = raw.get("close_time")
        if close_time is None:
            close_time = self._bucket_close_time(bucket_start, interval)
        else:
            close_time = int(close_time)
        open_p = float(raw.get("open", 0))
        high_p = float(raw.get("high", 0))
        low_p = float(raw.get("low", 0))
        close_p = float(raw.get("close", 0))
        tick_count = int(raw.get("tick_count", 0)) or 1
        return _default_candle(
            symbol=raw.get("symbol", self._symbol),
            interval=interval,
            open_time=bucket_start,
            close_time=close_time,
            open_price=open_p,
            high_price=high_p,
            low_price=low_p,
            close_price=close_p,
            tick_count=tick_count,
            is_closed=True,
        )

    # -------------------------------------------------------------------------
    # Main processing
    # -------------------------------------------------------------------------
    def process_tick(self, price: float, timestamp_ms: int) -> dict[str, Any]:
        """
        Process one live price update; update or create candles; finalize on bucket change.

        Args:
            price: Last/mid price for this tick.
            timestamp_ms: Timestamp of the tick in milliseconds.

        Returns:
            Dict with:
              - new_1m_closed: finalized 1m candle or None
              - new_5m_closed: finalized 5m candle or None
              - current_1m: current active 1m candle (copy)
              - current_5m: current active 5m candle (copy)
        """
        try:
            price = self._validate_price(price)
            timestamp_ms = self._validate_timestamp(timestamp_ms)
        except ValueError as e:
            logger.debug("Invalid tick ignored: %s", e)
            return self._tick_response(None, None)

        with self._lock:
            if self._last_processed_ts is not None and timestamp_ms < self._last_processed_ts:
                self._backward_ts_count += 1
                logger.debug(
                    "Backward timestamp ignored: ts=%s last=%s",
                    timestamp_ms,
                    self._last_processed_ts,
                )
                return self._tick_response(None, None)
            self._last_processed_ts = timestamp_ms
            self._total_ticks_processed += 1

            bucket_1m = self._get_1m_bucket_start(timestamp_ms)
            bucket_5m = self._get_5m_bucket_start(timestamp_ms)
            new_1m_closed: dict[str, Any] | None = None
            new_5m_closed: dict[str, Any] | None = None

            # --- 1m ---
            if self._current_1m is None:
                self._current_1m = self._new_candle("1m", bucket_1m, price)
            elif self._current_1m["open_time"] != bucket_1m:
                new_1m_closed = self._finalize_candle(self._current_1m)
                self._closed_1m.append(new_1m_closed)
                self._total_1m_closed += 1
                logger.info("1m candle closed open_time=%s", self._current_1m["open_time"])
                self._current_1m = self._new_candle("1m", bucket_1m, price)
            else:
                self._update_candle(self._current_1m, price)

            # --- 5m ---
            if self._current_5m is None:
                self._current_5m = self._new_candle("5m", bucket_5m, price)
            elif self._current_5m["open_time"] != bucket_5m:
                new_5m_closed = self._finalize_candle(self._current_5m)
                self._closed_5m.append(new_5m_closed)
                self._total_5m_closed += 1
                logger.info("5m candle closed open_time=%s", self._current_5m["open_time"])
                self._current_5m = self._new_candle("5m", bucket_5m, price)
            else:
                self._update_candle(self._current_5m, price)

            return self._tick_response(new_1m_closed, new_5m_closed)

    def _tick_response(
        self,
        new_1m_closed: dict[str, Any] | None,
        new_5m_closed: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build response dict; must be called with lock held."""
        return {
            "new_1m_closed": self._copy_candle(new_1m_closed),
            "new_5m_closed": self._copy_candle(new_5m_closed),
            "current_1m": self._copy_candle(self._current_1m),
            "current_5m": self._copy_candle(self._current_5m),
        }

    # -------------------------------------------------------------------------
    # Getters (thread-safe, return copies)
    # -------------------------------------------------------------------------
    def get_current_1m(self) -> dict[str, Any] | None:
        """Return a copy of the current active 1m candle, or None."""
        with self._lock:
            return self._copy_candle(self._current_1m)

    def get_current_5m(self) -> dict[str, Any] | None:
        """Return a copy of the current active 5m candle, or None."""
        with self._lock:
            return self._copy_candle(self._current_5m)

    def get_latest_closed_1m(self) -> dict[str, Any] | None:
        """Return a copy of the most recently closed 1m candle, or None."""
        with self._lock:
            if not self._closed_1m:
                return None
            return self._copy_candle(self._closed_1m[-1])

    def get_latest_closed_5m(self) -> dict[str, Any] | None:
        """Return a copy of the most recently closed 5m candle, or None."""
        with self._lock:
            if not self._closed_5m:
                return None
            return self._copy_candle(self._closed_5m[-1])

    def get_recent_closed_1m(self, n: int) -> list[dict[str, Any]]:
        """Return up to n most recent closed 1m candles (newest last)."""
        with self._lock:
            if n <= 0:
                return []
            size = len(self._closed_1m)
            take = min(n, size)
            return [self._copy_candle(self._closed_1m[i]) for i in range(size - take, size)]

    def get_recent_closed_5m(self, n: int) -> list[dict[str, Any]]:
        """Return up to n most recent closed 5m candles (newest last)."""
        with self._lock:
            if n <= 0:
                return []
            size = len(self._closed_5m)
            take = min(n, size)
            return [self._copy_candle(self._closed_5m[i]) for i in range(size - take, size)]

    def get_stats(self) -> dict[str, Any]:
        """Return builder statistics (ticks, closed counts, last timestamp, backward count)."""
        with self._lock:
            return {
                "total_ticks_processed": self._total_ticks_processed,
                "total_1m_closed": self._total_1m_closed,
                "total_5m_closed": self._total_5m_closed,
                "last_processed_ts": self._last_processed_ts,
                "backward_ts_ignored": self._backward_ts_count,
            }

    # -------------------------------------------------------------------------
    # Bootstrap
    # -------------------------------------------------------------------------
    def bootstrap_closed_candles(self, interval: str, candles: list[dict[str, Any]]) -> None:
        """
        Load historical closed candles into the appropriate deque.
        Does not overwrite or modify current active candles.

        Args:
            interval: "1m" or "5m".
            candles: List of candle dicts (e.g. from REST). Each may have
                open_time, open, high, low, close; close_time and tick_count optional.
        """
        if interval not in ("1m", "5m"):
            raise ValueError(f"Unsupported interval for bootstrap: {interval}")
        if not candles:
            return

        with self._lock:
            target = self._closed_1m if interval == "1m" else self._closed_5m
            for raw in candles:
                if not isinstance(raw, dict):
                    logger.debug("Bootstrap skip non-dict entry: %s", type(raw))
                    continue
                try:
                    normalized = self._normalize_bootstrap_candle(raw, interval)
                    if not all(normalized.get(k) is not None for k in ("open_time", "open", "high", "low", "close")):
                        logger.debug("Bootstrap skip incomplete candle: %s", list(normalized.keys()))
                        continue
                    target.append(normalized)
                except (ValueError, TypeError) as e:
                    logger.debug("Bootstrap skip invalid candle: %s", e)
            logger.info(
                "Bootstrap loaded %s %s candles; deque len=%s",
                len([c for c in candles if isinstance(c, dict)]),
                interval,
                len(target),
            )


# ---------------------------------------------------------------------------
# Demo / debug
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

    builder = CandleBuilder(symbol="BTC-USDC", max_closed_1m=100, max_closed_5m=100)

    # Use a fixed base so we can cross 1m and 5m boundaries predictably
    base_ms = 1700000000000  # 2023-11-14 22:13:20 UTC
    # 1m bucket: 1700000000000 -> 1700000040000 (22:13:00 - 22:14:00) -> 1700000040000 (22:14:00) next
    # 5m bucket: 1700000000000 -> 1700000100000 (22:10:00 - 22:15:00) -> 1700000100000 (22:15:00) next
    # So: start at 22:13:20, then move to 22:14:01 (new 1m), then 22:15:01 (new 1m and new 5m)

    ticks = [
        (100_000.0, base_ms),           # 22:13:20
        (100_001.0, base_ms + 15_000),  # 22:13:35
        (99_999.0, base_ms + 30_000),   # 22:13:50
        (100_002.0, base_ms + 45_000),  # 22:13:55
        (100_005.0, base_ms + 61_000),  # 22:14:01 -> new 1m bucket
        (100_010.0, base_ms + 90_000),  # 22:14:30
        (100_008.0, base_ms + 121_000), # 22:15:01 -> new 1m and new 5m bucket
        (100_012.0, base_ms + 150_000), # 22:15:30
    ]

    for price, ts in ticks:
        out = builder.process_tick(price, ts)
        if out["new_1m_closed"]:
            print("  [1m closed]", out["new_1m_closed"]["open_time"], out["new_1m_closed"])
        if out["new_5m_closed"]:
            print("  [5m closed]", out["new_5m_closed"]["open_time"], out["new_5m_closed"])

    print("\n--- Current 1m ---")
    print(builder.get_current_1m())
    print("\n--- Current 5m ---")
    print(builder.get_current_5m())
    print("\n--- Latest closed 1m ---")
    print(builder.get_latest_closed_1m())
    print("\n--- Latest closed 5m ---")
    print(builder.get_latest_closed_5m())
    print("\n--- Stats ---")
    print(builder.get_stats())
