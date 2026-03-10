"""
Historical candle bootstrap for Hyperliquid (REST fetch + validate + persist + builder).

This module provides HistoryBootstrapper for:
- Fetching recent historical candles from Hyperliquid REST API
- Validating and sanitizing candles
- Persisting to SQLite via StateStore
- Optionally bootstrapping a CandleBuilder instance

Read-only fetch; no trading, websocket, or order logic.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.candle_builder import CandleBuilder
from src.hyperliquid_client import HyperliquidClient
from src.state_store import StateStore

# Expected step between open_time values (ms)
STEP_1M_MS = 60_000
STEP_5M_MS = 300_000


def _setup_logging() -> logging.Logger:
    """Configure and return the module logger."""
    log = logging.getLogger("bootstrap_history")
    if not log.handlers:
        log.setLevel(logging.INFO)
        h = logging.StreamHandler()
        h.setLevel(logging.INFO)
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
        v = float(value)
        return v if v >= 0 else None
    try:
        v = float(str(value).strip())
        return v if v >= 0 else None
    except (ValueError, TypeError):
        return None


def _step_ms_for_interval(interval: str) -> int:
    """Return expected step in ms for interval."""
    if interval == "1m":
        return STEP_1M_MS
    if interval == "5m":
        return STEP_5M_MS
    raise ValueError(f"Unsupported interval: {interval}")


class HistoryBootstrapper:
    """
    Fetches historical candles from Hyperliquid REST, validates them,
    persists to SQLite, and can bootstrap a CandleBuilder.
    """

    def __init__(self, symbol: str = "BTC-USDC", db_path: str = "data/bot_state.db") -> None:
        """
        Initialize the bootstrapper.

        Args:
            symbol: Canonical symbol (e.g. BTC-USDC).
            db_path: Path to SQLite database for StateStore.
        """
        self.symbol = symbol
        self.db_path = db_path
        self._logger = _setup_logging()
        self._client = HyperliquidClient()
        self._store = StateStore(db_path=db_path)

    def fetch_recent_candles(
        self, interval: str, lookback_minutes: int
    ) -> list[dict[str, Any]]:
        """
        Fetch recent candles from Hyperliquid REST.

        Args:
            interval: "1m" or "5m".
            lookback_minutes: How many minutes back from now (UTC).

        Returns:
            List of normalized candle dicts (open_time, open, high, low, close, etc.).
        """
        if interval not in ("1m", "5m"):
            self._logger.warning("Unsupported interval %s; use 1m or 5m", interval)
            return []

        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (lookback_minutes * 60 * 1000)

        self._logger.info(
            "fetch_recent_candles start interval=%s lookback_min=%s start_ms=%s end_ms=%s",
            interval,
            lookback_minutes,
            start_ms,
            end_ms,
        )
        try:
            raw = self._client.get_candles(
                self.symbol, interval, start_ms=start_ms, end_ms=end_ms
            )
        except Exception as e:
            self._logger.exception("fetch_recent_candles failed: %s", e)
            return []

        # Normalize for our schema: add symbol, interval, close_time; ensure OHLC numeric
        step_ms = _step_ms_for_interval(interval)
        out: list[dict[str, Any]] = []
        for c in raw or []:
            if not isinstance(c, dict):
                continue
            open_time = c.get("open_time")
            if open_time is None:
                continue
            try:
                open_time_int = int(open_time)
            except (TypeError, ValueError):
                continue
            close_time = open_time_int + step_ms
            o = _safe_float(c.get("open"))
            h = _safe_float(c.get("high"))
            l = _safe_float(c.get("low"))
            cl = _safe_float(c.get("close"))
            if o is None or h is None or l is None or cl is None:
                continue
            out.append({
                "symbol": self.symbol,
                "interval": interval,
                "open_time": open_time_int,
                "close_time": close_time,
                "open": o,
                "high": h,
                "low": l,
                "close": cl,
                "tick_count": int(c.get("tick_count", 0)) if c.get("tick_count") is not None else 0,
                "is_closed": True,
            })
        self._logger.info(
            "fetch_recent_candles end interval=%s fetched=%s normalized=%s",
            interval,
            len(raw) if raw else 0,
            len(out),
        )
        return out

    def validate_candles(
        self, candles: list[dict[str, Any]], interval: str
    ) -> dict[str, Any]:
        """
        Inspect candles and return a validation summary.

        Returns dict with: total, duplicates, malformed, backward_order,
        spacing_issues, first_open_time, last_open_time.
        """
        step_ms = _step_ms_for_interval(interval)
        result: dict[str, Any] = {
            "total": len(candles),
            "duplicates": [],
            "malformed": 0,
            "backward_order": [],
            "spacing_issues": 0,
            "first_open_time": None,
            "last_open_time": None,
        }
        if not candles:
            return result

        seen: set[int] = set()
        prev_open: int | None = None
        malformed = 0

        for c in candles:
            if not isinstance(c, dict):
                malformed += 1
                continue
            ot = c.get("open_time")
            if ot is None:
                malformed += 1
                continue
            try:
                ot_int = int(ot)
            except (TypeError, ValueError):
                malformed += 1
                continue

            for key in ("open", "high", "low", "close"):
                val = c.get(key)
                if _safe_float(val) is None:
                    malformed += 1
                    break
            else:
                # No malformed OHLC in this row
                if ot_int in seen:
                    result["duplicates"].append(ot_int)
                    continue
                seen.add(ot_int)

                if prev_open is not None:
                    if ot_int < prev_open:
                        result["backward_order"].append({"prev": prev_open, "current": ot_int})
                    elif ot_int - prev_open != step_ms:
                        result["spacing_issues"] += 1
                prev_open = ot_int

        result["malformed"] = malformed
        sorted_opens = sorted(seen)
        if sorted_opens:
            result["first_open_time"] = sorted_opens[0]
            result["last_open_time"] = sorted_opens[-1]
        return result

    def filter_valid_closed_candles(
        self, candles: list[dict[str, Any]], interval: str
    ) -> list[dict[str, Any]]:
        """
        Keep only clean, closed candles: remove malformed and duplicate open_time,
        sort by open_time ascending.
        """
        step_ms = _step_ms_for_interval(interval)
        seen: set[int] = set()
        valid: list[dict[str, Any]] = []

        for c in candles:
            if not isinstance(c, dict):
                continue
            ot = c.get("open_time")
            if ot is None:
                continue
            try:
                ot_int = int(ot)
            except (TypeError, ValueError):
                continue
            if ot_int in seen:
                continue
            o = _safe_float(c.get("open"))
            h = _safe_float(c.get("high"))
            l = _safe_float(c.get("low"))
            cl = _safe_float(c.get("close"))
            if o is None or h is None or l is None or cl is None:
                continue
            if c.get("symbol") is None:
                c = {**c, "symbol": self.symbol}
            if c.get("interval") is None:
                c = {**c, "interval": interval}
            if c.get("close_time") is None:
                c = {**c, "close_time": ot_int + step_ms}
            if "tick_count" not in c or c.get("tick_count") is None:
                c = {**c, "tick_count": 0}
            if "is_closed" not in c:
                c = {**c, "is_closed": True}
            seen.add(ot_int)
            valid.append(c)

        valid.sort(key=lambda x: int(x["open_time"]))
        return valid

    def persist_candles(self, candles: list[dict[str, Any]]) -> int:
        """
        Save candles to StateStore via save_closed_candles.
        Returns number of candles attempted to persist.
        """
        if not candles:
            self._logger.info("persist_candles: no candles to persist")
            return 0
        try:
            self._store.save_closed_candles(candles)
            self._logger.info("persist_candles: attempted=%s", len(candles))
            return len(candles)
        except Exception as e:
            self._logger.exception("persist_candles failed: %s", e)
            raise

    def bootstrap_builder(
        self,
        builder: CandleBuilder,
        candles: list[dict[str, Any]],
        interval: str,
    ) -> dict[str, Any]:
        """
        Call builder.bootstrap_closed_candles(interval, candles).
        Returns summary dict with interval and count loaded.
        """
        summary: dict[str, Any] = {"interval": interval, "count_loaded": 0}
        if not candles:
            self._logger.info("bootstrap_builder: no candles for interval=%s", interval)
            return summary
        try:
            builder.bootstrap_closed_candles(interval, candles)
            summary["count_loaded"] = len(candles)
            self._logger.info(
                "bootstrap_builder: interval=%s count=%s",
                interval,
                summary["count_loaded"],
            )
        except Exception as e:
            self._logger.exception("bootstrap_builder failed interval=%s: %s", interval, e)
            summary["error"] = str(e)
        return summary

    def run_bootstrap(
        self,
        one_min_lookback_minutes: int = 120,
        five_min_lookback_minutes: int = 720,
    ) -> dict[str, Any]:
        """
        Fetch 1m and 5m candles, validate, sanitize, persist, and bootstrap
        a temporary CandleBuilder. Returns a full summary dict.
        """
        summary: dict[str, Any] = {
            "symbol": self.symbol,
            "1m": {"fetched": 0, "validated": {}, "kept": 0, "persisted": 0, "bootstrap": {}},
            "5m": {"fetched": 0, "validated": {}, "kept": 0, "persisted": 0, "bootstrap": {}},
            "errors": [],
        }

        self._logger.info(
            "run_bootstrap start symbol=%s 1m_lookback=%s 5m_lookback=%s",
            self.symbol,
            one_min_lookback_minutes,
            five_min_lookback_minutes,
        )

        # Fetch 1m
        candles_1m = self.fetch_recent_candles("1m", one_min_lookback_minutes)
        summary["1m"]["fetched"] = len(candles_1m)
        val_1m = self.validate_candles(candles_1m, "1m")
        summary["1m"]["validated"] = val_1m
        self._logger.info("run_bootstrap 1m validation: %s", val_1m)
        clean_1m = self.filter_valid_closed_candles(candles_1m, "1m")
        summary["1m"]["kept"] = len(clean_1m)

        # Fetch 5m
        candles_5m = self.fetch_recent_candles("5m", five_min_lookback_minutes)
        summary["5m"]["fetched"] = len(candles_5m)
        val_5m = self.validate_candles(candles_5m, "5m")
        summary["5m"]["validated"] = val_5m
        self._logger.info("run_bootstrap 5m validation: %s", val_5m)
        clean_5m = self.filter_valid_closed_candles(candles_5m, "5m")
        summary["5m"]["kept"] = len(clean_5m)

        # Persist
        try:
            n1 = self.persist_candles(clean_1m)
            summary["1m"]["persisted"] = n1
        except Exception as e:
            summary["errors"].append(f"persist 1m: {e}")
            self._logger.warning("persist 1m failed: %s", e)
        try:
            n5 = self.persist_candles(clean_5m)
            summary["5m"]["persisted"] = n5
        except Exception as e:
            summary["errors"].append(f"persist 5m: {e}")
            self._logger.warning("persist 5m failed: %s", e)

        # Bootstrap builder
        builder = CandleBuilder(symbol=self.symbol)
        try:
            summary["1m"]["bootstrap"] = self.bootstrap_builder(builder, clean_1m, "1m")
        except Exception as e:
            summary["errors"].append(f"bootstrap 1m: {e}")
        try:
            summary["5m"]["bootstrap"] = self.bootstrap_builder(builder, clean_5m, "5m")
        except Exception as e:
            summary["errors"].append(f"bootstrap 5m: {e}")

        self._logger.info("run_bootstrap end summary 1m_kept=%s 5m_kept=%s", len(clean_1m), len(clean_5m))
        return summary

    def close(self) -> None:
        """Close StateStore connection."""
        try:
            self._store.close()
        except Exception as e:
            self._logger.warning("close StateStore: %s", e)


def _print_summary(summary: dict[str, Any]) -> None:
    """Print a readable summary of run_bootstrap result."""
    print("\n" + "=" * 60)
    print("BOOTSTRAP SUMMARY")
    print("=" * 60)
    print(f"  Symbol: {summary.get('symbol', '?')}")
    for interval in ("1m", "5m"):
        data = summary.get(interval, {})
        print(f"  [{interval}] fetched={data.get('fetched', 0)} kept={data.get('kept', 0)} persisted={data.get('persisted', 0)}")
        v = data.get("validated", {})
        print(f"         validation: total={v.get('total')} duplicates={len(v.get('duplicates', []))} malformed={v.get('malformed')} spacing_issues={v.get('spacing_issues')}")
        b = data.get("bootstrap", {})
        print(f"         bootstrap: count_loaded={b.get('count_loaded', 0)}")
    if summary.get("errors"):
        print("  Errors:", summary["errors"])
    print("=" * 60 + "\n")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("src.state_store").setLevel(logging.WARNING)
    boot = HistoryBootstrapper(symbol="BTC-USDC", db_path="data/bot_state.db")
    try:
        summary = boot.run_bootstrap(
            one_min_lookback_minutes=120,
            five_min_lookback_minutes=720,
        )
        _print_summary(summary)
    except Exception as e:
        print(f"Bootstrap failed: {e}")
        raise
    finally:
        boot.close()
