"""
Paper trading integration runner for the Hyperliquid BTC-USDC 5m breakout strategy.

Connects WebSocket market data, CandleBuilder, BreakoutStrategyEngine, and StateStore.
Runs the strategy live in paper mode: replays today's 5m candles, feeds live prices
and closed 5m candles into the strategy, persists state, and logs events.

No live orders; strategy emits signals and state only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from src.breakout_strategy import BreakoutStrategyEngine
from src.candle_builder import CandleBuilder
from src.paper_trade_logger import PaperTradeLogger
from src.state_store import StateStore
from src.ws_marketdata import HyperliquidWSMarketData

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")
RUNNER_NAME = "run_breakout_paper"
DEFAULT_DB_PATH = "data/bot_state.db"
POLL_INTERVAL_SEC = 0.25
HEARTBEAT_INTERVAL_SEC = 5.0
RUNNER_STATS_INTERVAL_SEC = 30.0


def _setup_logging() -> logging.Logger:
    """Configure and return module logger."""
    log = logging.getLogger("run_breakout_paper")
    if not log.handlers:
        log.setLevel(logging.DEBUG)
        h = logging.StreamHandler()
        h.setLevel(logging.DEBUG)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        h.setFormatter(fmt)
        log.addHandler(h)
    return log


def _now_ms() -> int:
    """Current time in milliseconds since epoch."""
    return int(time.time() * 1000)


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
        if t < 1e12:
            t = t * 1000
        return t
    except (ValueError, TypeError):
        return None


def _utc_ms_to_ist_date_str(timestamp_ms: int) -> str:
    """Convert UTC ms to IST date string YYYY-MM-DD."""
    dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone(IST)
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Component initialization
# ---------------------------------------------------------------------------
def initialize_components(
    symbol: str = "BTC-USDC",
    db_path: str = DEFAULT_DB_PATH,
) -> tuple[StateStore, BreakoutStrategyEngine, CandleBuilder, HyperliquidWSMarketData]:
    """
    Create and return store, strategy, candle builder, and WebSocket client.
    Does not start the WebSocket; call ws.start() after replay.
    """
    store = StateStore(db_path=db_path)
    strategy = BreakoutStrategyEngine(symbol=symbol, db_path=db_path)
    candle_builder = CandleBuilder(symbol=symbol)
    ws = HyperliquidWSMarketData(symbol=symbol)
    return store, strategy, candle_builder, ws


# ---------------------------------------------------------------------------
# Replay today's 5m candles into strategy
# ---------------------------------------------------------------------------
def replay_today_candles(
    store: StateStore,
    strategy: BreakoutStrategyEngine,
    symbol: str,
    logger: logging.Logger,
    paper_logger: PaperTradeLogger | None = None,
) -> int:
    """
    Load today's (IST) closed 5m candles from DB and feed them sequentially
    into strategy.process_closed_5m_candle(). Returns count of candles replayed.
    """
    now_ms = _now_ms()
    today_ist_str = _utc_ms_to_ist_date_str(now_ms)

    # Fetch recent 5m candles (enough to cover one full session day: 90 x 5m)
    rows = store.get_recent_closed_candles(symbol=symbol, interval="5m", limit=200)
    if not rows:
        logger.info("replay_today_candles: no 5m candles in DB for today")
        return 0

    # Filter to today (IST) and sort by open_time ascending
    today_candles = []
    for r in rows:
        ot = r.get("open_time")
        if ot is None:
            continue
        try:
            open_time_ms = int(ot)
        except (TypeError, ValueError):
            continue
        row_date_ist = _utc_ms_to_ist_date_str(open_time_ms)
        if row_date_ist != today_ist_str:
            continue
        today_candles.append(r)

    today_candles.sort(key=lambda c: c.get("open_time") or 0)
    if not today_candles:
        logger.info("replay_today_candles: no 5m candles for today IST date=%s", today_ist_str)
        return 0

    logger.info("replay_today_candles: feeding %d candles for today IST=%s", len(today_candles), today_ist_str)
    for candle in today_candles:
        # Ensure candle dict has required keys (DB row may use different names)
        c = dict(candle)
        if "symbol" not in c:
            c["symbol"] = symbol
        if "interval" not in c:
            c["interval"] = "5m"
        evt = strategy.process_closed_5m_candle(c)
        if evt.get("pair_found"):
            logger.info("PAIR FOUND (replay) range_high=%s range_low=%s range_size=%s",
                        evt.get("range_high"), evt.get("range_low"), evt.get("range_size"))
        # Audit meaningful replay events (restart-safe via DB dedupe)
        if paper_logger is not None and (evt.get("new_day_reset") or evt.get("pair_found")):
            try:
                paper_logger.log_strategy_event(evt)
            except Exception as e:
                logger.warning("paper_logger replay audit failed: %s", e)

    return len(today_candles)


# ---------------------------------------------------------------------------
# Strategy event handling (logging + persistence)
# ---------------------------------------------------------------------------
def handle_strategy_event(
    event: dict[str, Any],
    strategy: BreakoutStrategyEngine,
    store: StateStore,
    logger: logging.Logger,
    paper_logger: PaperTradeLogger | None = None,
) -> str | None:
    """
    Handle a strategy result dict: log human-readable lines, persist state.
    Returns last_event_type for runner_stats (e.g. 'pair_found', 'entry', 'exit_sl').
    """
    if not event or not event.get("processed", True):
        return None

    last_type: str | None = None

    if event.get("new_day_reset"):
        logger.info("NEW DAY RESET date=%s", event.get("strategy_date_ist"))
        last_type = "new_day_reset"
    if event.get("pair_found"):
        logger.info("PAIR FOUND range_high=%s range_low=%s range_size=%s",
                    event.get("range_high"), event.get("range_low"), event.get("range_size"))
        last_type = "pair_found"
    entry = event.get("entry")
    if isinstance(entry, dict):
        sig = entry.get("signal")
        if sig == "long_entry":
            logger.info("ENTRY LONG price=%s sl=%s tp=%s",
                        entry.get("price"), entry.get("sl"), entry.get("tp"))
            last_type = "entry_long"
        elif sig == "short_entry":
            logger.info("ENTRY SHORT price=%s sl=%s tp=%s",
                        entry.get("price"), entry.get("sl"), entry.get("tp"))
            last_type = "entry_short"
    exit_evt = event.get("exit")
    if isinstance(exit_evt, dict):
        if exit_evt.get("exit") == "sl":
            logger.info("STOP LOSS side=%s price=%s sl_count=%s halted=%s",
                        exit_evt.get("side"), exit_evt.get("price"),
                        exit_evt.get("sl_count"), exit_evt.get("halted", False))
            last_type = "stop_loss"
        elif exit_evt.get("exit") == "tp":
            logger.info("TAKE PROFIT side=%s price=%s halted=%s",
                        exit_evt.get("side"), exit_evt.get("price"), exit_evt.get("halted", False))
            last_type = "take_profit"
    if event.get("halt_or_session_ended") or (isinstance(exit_evt, dict) and exit_evt.get("halted")):
        logger.info("TRADING HALTED for the day")
        # Session end is a one-shot transition event in the strategy; log it as a halt-type event.
        if event.get("session_ended") and last_type is None:
            last_type = "trading_halted"

    # Persist only when a meaningful event occurred (not on every tick)
    if last_type is not None:
        strategy.persist_state()
        if paper_logger is not None:
            try:
                paper_logger.log_strategy_event(event)
            except Exception as e:
                logger.warning("paper_logger failed: %s", e)
    return last_type


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------
class BreakoutPaperRunner:
    """
    Paper runner: WebSocket + CandleBuilder + BreakoutStrategyEngine + StateStore.
    Replays today's 5m candles, then feeds live prices and closed 5m candles to the strategy.
    """

    def __init__(
        self,
        symbol: str = "BTC-USDC",
        db_path: str = DEFAULT_DB_PATH,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
        heartbeat_interval_sec: float = HEARTBEAT_INTERVAL_SEC,
    ) -> None:
        self.symbol = symbol
        self.db_path = db_path
        self.poll_interval_sec = poll_interval_sec
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self._logger = _setup_logging()

        self._store: StateStore | None = None
        self._strategy: BreakoutStrategyEngine | None = None
        self._candle_builder: CandleBuilder | None = None
        self._ws: HyperliquidWSMarketData | None = None
        self._paper_logger: PaperTradeLogger | None = None

        self._running = False
        self._last_price: float | None = None
        self._last_ts_ms: int | None = None
        self._last_processed_5m_open_time: int | None = None

        self.loop_iterations = 0
        self.snapshots_seen = 0
        self.ticks_processed = 0
        self.duplicate_ticks_skipped = 0
        self.invalid_snapshots = 0
        self.last_tick_ts: int | None = None
        self.last_price_time: int | None = None
        self.last_candle_time: int | None = None
        self.last_event_type: str | None = None
        self.start_time: float | None = None
        self._last_heartbeat_time: float = 0.0
        self._last_runner_stats_time: float = 0.0

    def _extract_price_from_snapshot(self, snapshot: dict[str, Any]) -> tuple[float, int] | None:
        """Prefer trade (last_price), fallback to mid. Return (price, timestamp_ms) or None."""
        if not isinstance(snapshot, dict):
            return None
        price = _safe_float(snapshot.get("last_price"))
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
            ts_ms = _now_ms()
        return (price, ts_ms)

    def _should_process_tick(self, price: float, timestamp_ms: int) -> bool:
        """Avoid duplicate: only process if price or timestamp changed."""
        if self._last_price is None and self._last_ts_ms is None:
            return True
        if self._last_ts_ms == timestamp_ms and self._last_price == price:
            return False
        return True

    def _process_live_price(self, price: float, timestamp_ms: int) -> None:
        """Feed live price to strategy; on event, handle and persist."""
        if self._strategy is None:
            return
        result = self._strategy.process_live_price(price, timestamp_ms)
        self.last_price_time = timestamp_ms
        event_type = handle_strategy_event(result, self._strategy, self._store, self._logger, self._paper_logger)
        if event_type:
            self.last_event_type = event_type
        summary = self._strategy.get_compact_summary()
        if result.get("entry") or result.get("exit"):
            self._logger.info("strategy summary: %s", summary)

    def _process_closed_5m_candle(self, candle: dict[str, Any]) -> None:
        """Feed one closed 5m candle to strategy; avoid duplicate by open_time."""
        if self._strategy is None:
            return
        open_time = candle.get("open_time")
        if open_time is not None:
            try:
                ot = int(open_time)
                if self._last_processed_5m_open_time is not None and ot <= self._last_processed_5m_open_time:
                    return
                self._last_processed_5m_open_time = ot
            except (TypeError, ValueError):
                pass
        self.last_candle_time = open_time
        evt = self._strategy.process_closed_5m_candle(candle)
        event_type = handle_strategy_event(evt, self._strategy, self._store, self._logger, self._paper_logger)
        if event_type:
            self.last_event_type = event_type

    def _persist_runner_stats(self) -> None:
        """Update runner_stats with loop stats and meta (last_price_time, last_candle_time, etc.)."""
        if self._store is None:
            return
        stats = {
            "loop_iterations": self.loop_iterations,
            "snapshots_seen": self.snapshots_seen,
            "ticks_processed": self.ticks_processed,
            "duplicate_ticks_skipped": self.duplicate_ticks_skipped,
            "invalid_snapshots": self.invalid_snapshots,
            "last_tick_ts": self.last_tick_ts,
            "last_price_time": self.last_price_time,
            "last_candle_time": self.last_candle_time,
            "last_event_type": self.last_event_type,
            "runner_heartbeat": _now_ms(),
        }
        try:
            self._store.save_runner_stats(RUNNER_NAME, stats)
        except Exception as e:
            self._logger.warning("persist_runner_stats failed: %s", e)

    def _log_heartbeat(self) -> None:
        """Log compact summary and runner stats."""
        self._persist_runner_stats()
        if self._strategy:
            summary = self._strategy.get_compact_summary()
            self._logger.info("heartbeat strategy: %s", summary)
        self._logger.info(
            "heartbeat iterations=%s ticks=%s last_price_time=%s last_candle_time=%s last_event=%s",
            self.loop_iterations,
            self.ticks_processed,
            self.last_price_time,
            self.last_candle_time,
            self.last_event_type,
        )

    def start(self) -> None:
        """Initialize components, replay today's candles, start WebSocket, run loop."""
        self._logger.info("Breakout paper runner starting symbol=%s db=%s", self.symbol, self.db_path)
        self.start_time = time.time()
        self._last_heartbeat_time = time.time()
        self._last_runner_stats_time = time.time()
        self._running = True

        self._store, self._strategy, self._candle_builder, self._ws = initialize_components(
            symbol=self.symbol,
            db_path=self.db_path,
        )
        self._paper_logger = PaperTradeLogger(store=self._store, strategy_name="breakout_strategy", symbol=self.symbol)

        # Bootstrap candle builder with today's closed 5m (and 1m) so it does not re-emit them
        now_ms = _now_ms()
        today_ist_str = _utc_ms_to_ist_date_str(now_ms)
        for interval in ("5m", "1m"):
            rows = self._store.get_recent_closed_candles(symbol=self.symbol, interval=interval, limit=200)
            today_list = [r for r in rows if _utc_ms_to_ist_date_str(int(r.get("open_time") or 0)) == today_ist_str]
            today_list.sort(key=lambda c: c.get("open_time") or 0)
            if today_list:
                self._candle_builder.bootstrap_closed_candles(interval, today_list)
                self._logger.info("bootstrapped candle_builder with %d today %s candles", len(today_list), interval)

        # Replay today's 5m candles into strategy
        replayed = replay_today_candles(self._store, self._strategy, self.symbol, self._logger, self._paper_logger)
        self._logger.info("replay done: %d candles fed to strategy", replayed)
        # Replay run should also be audited for restart safety / traceability
        try:
            self._paper_logger.update_daily_summary(ist_date=today_ist_str)
        except Exception as e:
            self._logger.warning("paper_logger daily summary init failed: %s", e)

        # Set last_processed_5m_open_time from strategy so we don't re-feed replayed candles
        snap = self._strategy.get_state_snapshot()
        self._last_processed_5m_open_time = snap.get("last_processed_5m_open_time")

        self._ws.start()
        self._logger.info("WebSocket started; entering main loop")

        try:
            while self._running:
                self.loop_iterations += 1
                try:
                    snapshot = self._ws.get_latest_snapshot()
                    self.snapshots_seen += 1

                    # Extract price and feed candle builder
                    tick = self._extract_price_from_snapshot(snapshot)
                    if tick is None:
                        self.invalid_snapshots += 1
                        time.sleep(self.poll_interval_sec)
                        continue

                    price, timestamp_ms = tick
                    if not self._should_process_tick(price, timestamp_ms):
                        self.duplicate_ticks_skipped += 1
                        time.sleep(self.poll_interval_sec)
                        continue

                    self._last_price = price
                    self._last_ts_ms = timestamp_ms
                    self.ticks_processed += 1
                    self.last_tick_ts = timestamp_ms

                    # Update candle builder; detect closed 5m
                    try:
                        result = self._candle_builder.process_tick(price, timestamp_ms)
                    except Exception as e:
                        self._logger.warning("candle_builder process_tick error: %s", e)
                        time.sleep(self.poll_interval_sec)
                        continue

                    # Persist and emit closed candles (optional; strategy gets them from builder)
                    new_5m = result.get("new_5m_closed")
                    if new_5m and isinstance(new_5m, dict):
                        try:
                            self._store.save_closed_candle(new_5m)
                        except Exception as e:
                            self._logger.debug("save_closed_candle: %s", e)
                        self._process_closed_5m_candle(new_5m)

                    # Feed live price to strategy every time we have a new tick
                    self._process_live_price(price, timestamp_ms)

                    # Heartbeat
                    now = time.time()
                    if now - self._last_heartbeat_time >= self.heartbeat_interval_sec:
                        self._last_heartbeat_time = now
                        self._log_heartbeat()
                    if now - self._last_runner_stats_time >= RUNNER_STATS_INTERVAL_SEC:
                        self._last_runner_stats_time = now
                        self._persist_runner_stats()

                except Exception as e:
                    self._logger.exception("main_loop error: %s", e)
                time.sleep(self.poll_interval_sec)

        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        """Graceful shutdown: persist state, close WS, close store."""
        self._logger.info("Shutting down breakout paper runner...")
        self._running = False
        if self._strategy:
            self._strategy.persist_state()
        self._persist_runner_stats()
        if self._ws:
            self._ws.stop()
        if self._strategy:
            self._strategy.close()
        if self._store:
            self._store.close()
        uptime = (time.time() - self.start_time) if self.start_time else 0
        self._logger.info(
            "Shutdown complete uptime_sec=%.1f iterations=%s ticks=%s",
            uptime, self.loop_iterations, self.ticks_processed,
        )

    def stop(self) -> None:
        """Request stop (e.g. from KeyboardInterrupt)."""
        self._logger.info("Stop requested")
        self._running = False
        if self._ws:
            self._ws.stop()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def main() -> None:
    """Run the paper breakout runner until KeyboardInterrupt."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # Reduce noise from dependencies
    logging.getLogger("ws_marketdata").setLevel(logging.WARNING)
    logging.getLogger("breakout_strategy").setLevel(logging.INFO)

    runner = BreakoutPaperRunner(symbol="BTC-USDC", db_path=DEFAULT_DB_PATH)
    try:
        runner.start()
    except KeyboardInterrupt:
        print("\nKeyboardInterrupt: stopping breakout paper runner...")
        runner.stop()
        runner._shutdown()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
