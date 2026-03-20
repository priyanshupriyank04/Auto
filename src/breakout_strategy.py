"""
Breakout strategy engine (paper/signal mode) for Hyperliquid BTC-USDC.

Strategy rules (PineScript-aligned):
- 5-minute candles, IST (Asia/Kolkata)
- Weekdays only; session 08:00–15:30 IST
- Trades/signals only after 08:10 IST
- First valid opposite-color pair (green→red or red→green) after 08:00 defines range
- Breakout above range_high = paper long; below range_low = paper short
- TP = 1x per day ( 4 times range) then halt; SL = 3x then halt
- No live orders; state and signal generation only
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from src.state_store import StateStore

IST = ZoneInfo("Asia/Kolkata")
COMPONENT_NAME = "breakout_strategy"

# Session: 08:00 to 15:30 IST; first trade allowed after 08:10 IST
SESSION_START_HOUR, SESSION_START_MINUTE = 8, 0
SESSION_END_HOUR, SESSION_END_MINUTE = 15, 30
FIRST_TRADE_HOUR, FIRST_TRADE_MINUTE = 8, 10

# State names
WAITING_FOR_NEW_DAY = "WAITING_FOR_NEW_DAY"
WAITING_FOR_SESSION_START = "WAITING_FOR_SESSION_START"
SCANNING_FOR_PAIR = "SCANNING_FOR_PAIR"
RANGE_DEFINED = "RANGE_DEFINED"
LONG_ACTIVE = "LONG_ACTIVE"
SHORT_ACTIVE = "SHORT_ACTIVE"
HALTED_FOR_DAY = "HALTED_FOR_DAY"
SESSION_ENDED = "SESSION_ENDED"
WEEKEND_NO_TRADE = "WEEKEND_NO_TRADE"

MAX_EVENT_LOG = 50


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("breakout_strategy")
    if not log.handlers:
        log.setLevel(logging.DEBUG)
        h = logging.StreamHandler()
        h.setLevel(logging.DEBUG)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        log.addHandler(h)
    return log


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return None


@dataclass
class DailyState:
    """Internal daily strategy state (IST day)."""
    strategy_date_ist: str = ""
    weekday_allowed: bool = False
    session_started: bool = False
    pair_found: bool = False
    breakout_ready: bool = False
    no_trade_reason: str = ""
    pair_first_candle: dict[str, Any] | None = None
    pair_second_candle: dict[str, Any] | None = None
    previous_5m_candle: dict[str, Any] | None = None
    range_high: float | None = None
    range_low: float | None = None
    range_size: float | None = None
    current_state: str = WAITING_FOR_NEW_DAY
    virtual_in_trade: bool = False
    virtual_side: str = ""
    virtual_entry: float | None = None
    virtual_sl: float | None = None
    virtual_tp: float | None = None
    tp_hit: bool = False
    sl_count: int = 0
    halted_for_day: bool = False
    session_ended: bool = False
    breakout_armed: bool = False  # True only after price is seen inside [range_low, range_high]
    last_processed_5m_open_time: int | None = None
    last_live_ts_ms: int | None = None
    event_log: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_EVENT_LOG))


class PositionManager:
    """Handles size calculations and exchange constraints."""
    def __init__(self, min_order_usd: float = 10.0, pct_of_equity: float = 0.1):
        self.min_order_usd = min_order_usd
        self.pct_of_equity = pct_of_equity

    def calculate_size(self, equity: float, price: float) -> float:
        """Calculate order size in BTC based on user rules and account equity."""
        # Use pct if set, otherwise use fixed min_order_usd
        target_usd = equity * self.pct_of_equity
        if target_usd < self.min_order_usd:
            target_usd = self.min_order_usd
        
        # Note: We used to cap by equity here (min(target_usd, equity)), but 
        # removed it per user request to ensure fixed $11 size even if local check shows 0.
        
        # Convert to BTC size
        if price <= 0:
            return 0.0
        
        size_btc = target_usd / price
        
        # Hyperliquid BTC-USDC precision is up to 5 decimals for BTC
        # We round UP to 5 decimals to ensure we don't fall below the minimum USD requirement
        import math
        factor = 10**5
        size_btc = math.ceil(size_btc * factor) / factor
        
        return size_btc


class BreakoutStrategyEngine:
    """
    Paper-only breakout strategy: 5m candles IST, first opposite-color pair
    defines range; breakout long/short with TP/SL and daily halt rules.
    """

    def __init__(self, symbol: str = "BTC-USDC", db_path: str = "data/bot_state.db", target_date: str | None = None) -> None:
        self.symbol = symbol
        self._tz = IST
        self._logger = _setup_logging()
        self._store = StateStore(db_path=db_path)
        self._state = DailyState()
        self._target_date = target_date  # If set, strictly trade this YYYY-MM-DD only
        # Initialized to $11 fixed, 0% equity per user request
        self._pos_manager = PositionManager(min_order_usd=12.0, pct_of_equity=0.0)

    def _get_persistence_key(self, date_ist: str) -> str:
        """Dynamic key based on date for state isolation."""
        if not date_ist:
            return COMPONENT_NAME
        return f"{COMPONENT_NAME}_{date_ist}"

    def _round_price(self, price: float | None) -> float | None:
        """Round price to exchange tick size (1.0 for BTC on Hyperliquid)."""
        if price is None:
            return None
        return float(round(price, 0))

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    def _utc_ms_to_ist_datetime(self, timestamp_ms: int) -> datetime:
        """Convert UTC ms to datetime in IST."""
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone(self._tz)

    def _is_weekday_ist(self, dt_ist) -> bool:
        """Monday=0, Sunday=6. Weekday = 0-4."""
        # return dt_ist.weekday() < 5
        return True

    def _minutes_since_midnight_ist(self, dt_ist) -> int:
        return dt_ist.hour * 60 + dt_ist.minute

    def _is_in_session_ist(self, dt_ist) -> bool:
        """True if time is in [08:00, 15:30) IST."""
        m = self._minutes_since_midnight_ist(dt_ist)
        start_m = SESSION_START_HOUR * 60 + SESSION_START_MINUTE  # 480
        end_m = SESSION_END_HOUR * 60 + SESSION_END_MINUTE  # 930
        return start_m <= m < end_m

    def _is_after_session_start_ist(self, dt_ist) -> bool:
        """True if >= 08:00 IST."""
        m = self._minutes_since_midnight_ist(dt_ist)
        start_m = SESSION_START_HOUR * 60 + SESSION_START_MINUTE
        return m >= start_m

    def _is_after_first_trade_time_ist(self, dt_ist) -> bool:
        """True if >= 08:10 IST."""
        m = self._minutes_since_midnight_ist(dt_ist)
        first_m = FIRST_TRADE_HOUR * 60 + FIRST_TRADE_MINUTE  # 490
        return m >= first_m

    def _is_after_session_end_ist(self, dt_ist) -> bool:
        """True if >= 15:30 IST."""
        m = self._minutes_since_midnight_ist(dt_ist)
        end_m = SESSION_END_HOUR * 60 + SESSION_END_MINUTE
        return m >= end_m

    @staticmethod
    def _is_green(candle: dict) -> bool:
        c = _safe_float(candle.get("close"))
        o = _safe_float(candle.get("open"))
        if c is None or o is None:
            return False
        return c > o

    @staticmethod
    def _is_red(candle: dict) -> bool:
        c = _safe_float(candle.get("close"))
        o = _safe_float(candle.get("open"))
        if c is None or o is None:
            return False
        return c < o

    def _evaluate_pair_condition(self, prev_candle: dict, curr_candle: dict) -> bool:
        """True if first is green and second red, or first red and second green."""
        g1, r1 = self._is_green(prev_candle), self._is_red(prev_candle)
        g2, r2 = self._is_green(curr_candle), self._is_red(curr_candle)
        return (g1 and r2) or (r1 and g2)

    def _set_range_from_pair(self, prev_candle: dict, curr_candle: dict) -> None:
        h1 = _safe_float(prev_candle.get("high")) or 0.0
        h2 = _safe_float(curr_candle.get("high")) or 0.0
        l1 = _safe_float(prev_candle.get("low")) or 0.0
        l2 = _safe_float(curr_candle.get("low")) or 0.0
        # Round range extremes to nearest integer for BTC tick size compliance
        self._state.range_high = self._round_price(max(h1, h2))
        self._state.range_low = self._round_price(min(l1, l2))
        self._state.range_size = self._state.range_high - self._state.range_low
        self._state.pair_first_candle = dict(prev_candle)
        self._state.pair_second_candle = dict(curr_candle)
        # First range identification: Auto-arm for first trade
        self._state.breakout_armed = True
        self._logger.info("breakout_armed: first morning range identified [%.2f, %.2f]", 
                         self._state.range_low, self._state.range_high)

    def _record_event(self, event_type: str, payload: dict | None = None) -> None:
        entry = {"type": event_type, "ts_ms": self._now_ms()}
        if payload:
            entry["payload"] = payload
        self._state.event_log.append(entry)

    def _state_transition(self, new_state: str, reason: str) -> None:
        old = self._state.current_state
        self._state.current_state = new_state
        self._logger.info("state_transition: %s -> %s (%s)", old, new_state, reason)
        self._record_event("state_transition", {"from": old, "to": new_state, "reason": reason})

    def reset_for_new_day(self, dt_utc_ms: int) -> None:
        """Compute IST date from UTC ms and reset all daily state."""
        dt_ist = self._utc_ms_to_ist_datetime(dt_utc_ms)
        date_str = dt_ist.strftime("%Y-%m-%d")
        
        # If a target date is forced, ignore all other dates
        if self._target_date and date_str != self._target_date:
            return

        if self._state.strategy_date_ist == date_str:
            return
        self._logger.info("reset_for_new_day: IST date=%s", date_str)
        self._state = DailyState()
        self._state.strategy_date_ist = date_str
        self._state.weekday_allowed = self._is_weekday_ist(dt_ist)
        if not self._state.weekday_allowed:
            self._state.current_state = WEEKEND_NO_TRADE
            self._state.no_trade_reason = "weekend"
        elif self._is_after_session_end_ist(dt_ist):
            self._state.current_state = SESSION_ENDED
            self._state.session_ended = True
            self._state.no_trade_reason = "session_ended"
        elif not self._is_in_session_ist(dt_ist):
            self._state.current_state = WAITING_FOR_SESSION_START
            self._state.no_trade_reason = "before_session"
        else:
            self._state.session_started = True
            self._state.current_state = SCANNING_FOR_PAIR
        self._record_event("new_day_reset", {"strategy_date_ist": date_str, "weekday": self._state.weekday_allowed})

    def process_closed_5m_candle(self, candle: dict) -> dict[str, Any]:
        """
        Process one newly closed 5m candle: IST day reset, session checks,
        scan for first opposite-color pair, define range.
        """
        event: dict[str, Any] = {"processed": True}
        if not candle or not isinstance(candle, dict):
            event["processed"] = False
            event["reason"] = "invalid_candle"
            return event

        open_time = candle.get("open_time")
        if open_time is None:
            event["reason"] = "missing_open_time"
            return event
        try:
            open_time_int = int(open_time)
        except (TypeError, ValueError):
            event["reason"] = "invalid_open_time"
            return event

        # Duplicate check
        if self._state.last_processed_5m_open_time is not None and open_time_int <= self._state.last_processed_5m_open_time:
            self._logger.debug("duplicate_ignored open_time=%s", open_time_int)
            event["duplicate_ignored"] = True
            event["open_time"] = open_time_int
            return event

        dt_ist = self._utc_ms_to_ist_datetime(open_time_int)
        date_str = dt_ist.strftime("%Y-%m-%d")

        # TARGET DATE FILTER: If we have a target date, strictly skip anything else
        if self._target_date and date_str != self._target_date:
            self._logger.debug("skipping_candle_wrong_date open_time=%s date=%s target=%s", 
                               open_time_int, date_str, self._target_date)
            event["processed"] = False
            event["reason"] = "wrong_date"
            return event

        # New day reset if IST date changed
        if self._state.strategy_date_ist != date_str:
            self.reset_for_new_day(open_time_int)
            event["new_day_reset"] = True
            event["strategy_date_ist"] = date_str
            if self._state.current_state == WEEKEND_NO_TRADE:
                event["weekend_no_trade"] = True
                return event
            if self._state.current_state == SESSION_ENDED:
                event["session_ended"] = True
                return event

        if self._state.current_state == WEEKEND_NO_TRADE:
            event["weekend_no_trade"] = True
            return event
        if self._state.halted_for_day or self._state.session_ended:
            event["halt_or_session_ended"] = True
            return event

        # Session window: candle must be inside [08:00, 15:30) IST
        if not self._is_in_session_ist(dt_ist):
            self._logger.debug("candle_outside_session open_time=%s", open_time_int)
            event["outside_session"] = True
            self._state.last_processed_5m_open_time = open_time_int
            return event

        if not self._state.session_started:
            self._state.session_started = True
            self._state.current_state = SCANNING_FOR_PAIR

        # Scanning for first valid pair
        if not self._state.pair_found:
            prev = self._state.previous_5m_candle
            self._state.previous_5m_candle = dict(candle)
            self._state.last_processed_5m_open_time = open_time_int

            if prev is None:
                event["pair_scan"] = "no_previous"
                return event

            # Both candles must be after session start
            prev_ot = prev.get("open_time")
            if prev_ot is not None:
                prev_dt = self._utc_ms_to_ist_datetime(int(prev_ot))
                if not self._is_after_session_start_ist(prev_dt):
                    event["pair_scan"] = "prev_before_session"
                    return event
            if not self._is_after_session_start_ist(dt_ist):
                event["pair_scan"] = "curr_before_session"
                return event

            if self._evaluate_pair_condition(prev, candle):
                self._set_range_from_pair(prev, candle)
                self._state.pair_found = True
                self._state.breakout_ready = True
                self._state_transition(RANGE_DEFINED, "first_opposite_pair")
                event["pair_found"] = True
                event["range_high"] = self._state.range_high
                event["range_low"] = self._state.range_low
                event["range_size"] = self._state.range_size
                self._record_event("pair_found", {"range_high": self._state.range_high, "range_low": self._state.range_low})
            else:
                event["pair_invalid_continue_scan"] = True
                self._logger.debug("pair_invalid prev_open_time=%s curr_open_time=%s", prev.get("open_time"), open_time_int)
            return event

        # Pair already found; just update last processed
        self._state.last_processed_5m_open_time = open_time_int
        event["pair_already_found"] = True
        return event

    def _enter_virtual_long(self, price: float, timestamp_ms: int, equity: float | None = None) -> dict[str, Any]:
        if self._state.range_low is None or self._state.range_size is None:
            return {}
        p = self._round_price(price)
        self._state.virtual_in_trade = True
        self._state.virtual_side = "long"
        self._state.virtual_entry = p
        self._state.virtual_sl = self._state.range_low
        self._state.virtual_tp = self._round_price(self._state.range_high + 4.0 * self._state.range_size)
        self._state.breakout_armed = False # Reset arming for next trade (if SL hit)
        self._state_transition(LONG_ACTIVE, "breakout_above_range")
        self._record_event("paper_long_entry", {"price": p, "sl": self._state.virtual_sl, "tp": self._state.virtual_tp})
        self._logger.info("paper_long_entry price=%.2f sl=%.2f tp=%.2f", p, self._state.virtual_sl, self._state.virtual_tp)
        
        size = self._pos_manager.calculate_size(equity if equity is not None else 1000.0, p)
        return {"signal": "long_entry", "price": p, "sl": self._state.virtual_sl, "tp": self._state.virtual_tp, "size": size}

    def _enter_virtual_short(self, price: float, timestamp_ms: int, equity: float | None = None) -> dict[str, Any]:
        if self._state.range_high is None or self._state.range_size is None:
            return {}
        p = self._round_price(price)
        self._state.virtual_in_trade = True
        self._state.virtual_side = "short"
        self._state.virtual_entry = p
        self._state.virtual_sl = self._state.range_high
        self._state.virtual_tp = self._round_price(self._state.range_low - 4.0 * self._state.range_size)
        self._state.breakout_armed = False # Reset arming for next trade (if SL hit)
        self._state_transition(SHORT_ACTIVE, "breakout_below_range")
        self._record_event("paper_short_entry", {"price": p, "sl": self._state.virtual_sl, "tp": self._state.virtual_tp})
        self._logger.info("paper_short_entry price=%.2f sl=%.2f tp=%.2f", p, self._state.virtual_sl, self._state.virtual_tp)
        
        size = self._pos_manager.calculate_size(equity if equity is not None else 1000.0, p)
        return {"signal": "short_entry", "price": p, "sl": self._state.virtual_sl, "tp": self._state.virtual_tp, "size": size}

    def _check_virtual_trade_exit(self, price: float, timestamp_ms: int) -> dict | None:
        """Check TP/SL; return event dict if exit, else None."""
        if not self._state.virtual_in_trade or self._state.virtual_sl is None or self._state.virtual_tp is None:
            return None
        if self._state.range_high is None or self._state.range_low is None or (self._state.range_size or 0) <= 0:
            return None

        side = self._state.virtual_side
        p = price
        rs = self._state.range_size

        # --- Trailing SL logic (Step-wise) ---
        # For every 1x range_size profit, move SL in favor of trade by 1x range_size
        if side == "long":
            # k = 1 at 1x profit, k = 2 at 2x profit, etc.
            k = int((p - self._state.range_high) / rs)
            if k >= 1:
                # new_sl: at 1x profit (105), sl moves to 100. at 2x profit (110), sl moves to 105.
                new_sl = self._round_price(self._state.range_high + (k - 1) * rs)
                if new_sl > self._state.virtual_sl:
                    self._logger.info("trailing_sl_updated (long) to %.2f (milestone %dx profit)", new_sl, k)
                    self._state.virtual_sl = new_sl
                    self._record_event("trailing_sl_update", {"side": "long", "new_sl": new_sl, "milestone": k})
                    return {"trailing_update": True, "side": "long", "new_sl": new_sl}

        elif side == "short":
            k = int((self._state.range_low - p) / rs)
            if k >= 1:
                new_sl = self._round_price(self._state.range_low - (k - 1) * rs)
                if new_sl < self._state.virtual_sl:
                    self._logger.info("trailing_sl_updated (short) to %.2f (milestone %dx profit)", new_sl, k)
                    self._state.virtual_sl = new_sl
                    self._record_event("trailing_sl_update", {"side": "short", "new_sl": new_sl, "milestone": k})
                    return {"trailing_update": True, "side": "short", "new_sl": new_sl}

        # --- Exit checks ---
        if side == "long":
            if p <= self._state.virtual_sl:
                self._state.virtual_in_trade = False
                self._state.sl_count += 1
                self._record_event("paper_sl_hit", {"side": "long", "price": p, "sl_count": self._state.sl_count})
                self._logger.info("paper_sl_hit long price=%.2f sl_count=%s", p, self._state.sl_count)
                if self._state.sl_count >= 3:
                    self._state.halted_for_day = True
                    self._state_transition(HALTED_FOR_DAY, "3_sl_hits")
                    return {"exit": "sl", "side": "long", "price": p, "entry_price": self._state.virtual_entry, "sl_count": self._state.sl_count, "halted": True}
                self._state.current_state = RANGE_DEFINED
                return {"exit": "sl", "side": "long", "price": p, "entry_price": self._state.virtual_entry, "sl_count": self._state.sl_count}
            if p >= self._state.virtual_tp:
                self._state.virtual_in_trade = False
                self._state.tp_hit = True
                self._state.halted_for_day = True
                self._state_transition(HALTED_FOR_DAY, "tp_hit")
                self._record_event("paper_tp_hit", {"side": "long", "price": p})
                self._logger.info("paper_tp_hit long price=%.2f", p)
                return {"exit": "tp", "side": "long", "price": p, "entry_price": self._state.virtual_entry, "halted": True}
        else:  # short
            if p >= self._state.virtual_sl:
                self._state.virtual_in_trade = False
                self._state.sl_count += 1
                self._record_event("paper_sl_hit", {"side": "short", "price": p, "sl_count": self._state.sl_count})
                self._logger.info("paper_sl_hit short price=%.2f sl_count=%s", p, self._state.sl_count)
                if self._state.sl_count >= 3:
                    self._state.halted_for_day = True
                    self._state_transition(HALTED_FOR_DAY, "3_sl_hits")
                    return {"exit": "sl", "side": "short", "price": p, "entry_price": self._state.virtual_entry, "sl_count": self._state.sl_count, "halted": True}
                self._state.current_state = RANGE_DEFINED
                return {"exit": "sl", "side": "short", "price": p, "entry_price": self._state.virtual_entry, "sl_count": self._state.sl_count}
            if p <= self._state.virtual_tp:
                self._state.virtual_in_trade = False
                self._state.tp_hit = True
                self._state.halted_for_day = True
                self._state_transition(HALTED_FOR_DAY, "tp_hit")
                self._record_event("paper_tp_hit", {"side": "short", "price": p})
                self._logger.info("paper_tp_hit short price=%.2f", p)
                return {"exit": "tp", "side": "short", "price": p, "entry_price": self._state.virtual_entry, "halted": True}
        return None

    def process_live_price(self, price: float, timestamp_ms: int, equity: float | None = None) -> dict[str, Any]:
        """
        Process live price: after 08:10 IST and breakout_ready, check entry/TP/SL.
        Returns event dict for signal/trade changes.
        """
        result: dict[str, Any] = {"processed": True}
        p = _safe_float(price)
        if p is None:
            result["processed"] = False
            result["reason"] = "invalid_price"
            return result

        dt_ist = self._utc_ms_to_ist_datetime(timestamp_ms)
        date_str = dt_ist.strftime("%Y-%m-%d")

        # TARGET DATE FILTER
        if self._target_date and date_str != self._target_date:
            result["processed"] = False
            result["reason"] = "wrong_date"
            return result

        if self._state.strategy_date_ist != date_str:
            self.reset_for_new_day(timestamp_ms)
            result["new_day_reset"] = True
            if self._state.current_state == WEEKEND_NO_TRADE:
                return result

        if self._state.halted_for_day:
            result["halt_or_session_ended"] = True
            return result
        
        # Session end check
        if self._is_after_session_end_ist(dt_ist):
            if not self._state.session_ended:
                self._state.session_ended = True
                end_time_str = f"{SESSION_END_HOUR:02d}:{SESSION_END_MINUTE:02d}"
                self._state_transition(SESSION_ENDED, f"session_end_{end_time_str}")
                result["session_ended"] = True
                
                # If we are in a trade, we must close it immediately at market
                if self._state.virtual_in_trade:
                    self._logger.warning("Session ended at %s IST while in trade! Triggering MARKET CLOSE.", end_time_str)
                    self._state.virtual_in_trade = False
                    self._record_event("market_close_session_end", {"price": p, "side": self._state.virtual_side})
                    result["exit"] = {
                        "exit": "market_close",
                        "side": self._state.virtual_side,
                        "price": p,
                        "reason": "session_end"
                    }
            else:
                result["session_ended"] = True
            return result

        self._state.last_live_ts_ms = timestamp_ms

        # Active trade: check TP/SL first
        exit_evt = self._check_virtual_trade_exit(p, timestamp_ms)
        if exit_evt:
            if exit_evt.get("trailing_update"):
                result["trailing_update"] = exit_evt
            else:
                result["exit"] = exit_evt
            return result

        # No active trade: check for arming and breakout entry
        if not self._state.virtual_in_trade:
            # Skip if no range defined yet
            if self._state.range_low is None or self._state.range_high is None:
                return result
            
            # 1. ARMING: Price must be seen inside the range to arm the trigger
            if self._state.range_low <= p <= self._state.range_high:
                if not self._state.breakout_armed:
                    self._state.breakout_armed = True
                    self._logger.info("breakout_armed: price %.2f inside range [%.2f, %.2f]", 
                                     p, self._state.range_low, self._state.range_high)
                    self._record_event("breakout_armed", {"price": p})
                    result["armed_update"] = True

            # 2. TRIGGER: Only enter if armed
            if self._state.breakout_armed:
                if p > self._state.range_high:
                    result["entry"] = self._enter_virtual_long(p, timestamp_ms, equity=equity)
                elif p < self._state.range_low:
                    result["entry"] = self._enter_virtual_short(p, timestamp_ms, equity=equity)
            else:
                # Still waiting for price to enter the range
                if self.loop_iterations_local % 40 == 0: # Log occasionally
                     self._logger.debug("waiting_for_range_entry: price %.2f range [%.2f, %.2f]", 
                                      p, self._state.range_low, self._state.range_high)


        return result

    # Helper for logging control
    loop_iterations_local: int = 0

    def get_state_snapshot(self) -> dict[str, Any]:
        """Return full strategy state as a plain dict."""
        s = self._state
        return {
            "strategy_date_ist": s.strategy_date_ist,
            "weekday_allowed": s.weekday_allowed,
            "session_started": s.session_started,
            "pair_found": s.pair_found,
            "breakout_ready": s.breakout_ready,
            "no_trade_reason": s.no_trade_reason,
            "pair_first_candle": s.pair_first_candle,
            "pair_second_candle": s.pair_second_candle,
            "previous_5m_candle": s.previous_5m_candle,
            "range_high": s.range_high,
            "range_low": s.range_low,
            "range_size": s.range_size,
            "current_state": s.current_state,
            "virtual_in_trade": s.virtual_in_trade,
            "virtual_side": s.virtual_side,
            "virtual_entry": s.virtual_entry,
            "virtual_sl": s.virtual_sl,
            "virtual_tp": s.virtual_tp,
            "tp_hit": s.tp_hit,
            "sl_count": s.sl_count,
            "halted_for_day": s.halted_for_day,
            "session_ended": s.session_ended,
            "breakout_armed": s.breakout_armed,
            "last_processed_5m_open_time": s.last_processed_5m_open_time,
            "last_live_ts_ms": s.last_live_ts_ms,
            "event_log": list(s.event_log),
        }

    def get_compact_summary(self) -> dict[str, Any]:
        """Log-friendly compact strategy summary."""
        s = self._state
        return {
            "state": s.current_state,
            "date_ist": s.strategy_date_ist,
            "weekday": s.weekday_allowed,
            "pair_found": s.pair_found,
            "breakout_ready": s.breakout_ready,
            "range_high": s.range_high,
            "range_low": s.range_low,
            "range_size": s.range_size,
            "in_trade": s.virtual_in_trade,
            "side": s.virtual_side,
            "tp_hit": s.tp_hit,
            "sl_count": s.sl_count,
            "halted": s.halted_for_day,
        }

    def is_tradable_now(self, timestamp_ms: int) -> bool:
        """True if IST weekday, in session, after 08:10, not halted, pair_found and breakout_ready."""
        dt_ist = self._utc_ms_to_ist_datetime(timestamp_ms)
        date_str = dt_ist.strftime("%Y-%m-%d")
        if self._state.strategy_date_ist != date_str:
            return False
        if not self._state.weekday_allowed:
            return False
        if self._state.halted_for_day or self._state.session_ended:
            return False
        if not self._state.pair_found or not self._state.breakout_ready:
            return False
        if not self._is_after_first_trade_time_ist(dt_ist):
            return False
        if self._is_after_session_end_ist(dt_ist):
            return False
        return self._is_in_session_ist(dt_ist)

    def persist_state(self) -> None:
        """Save current strategy state to SQLite via StateStore component_status."""
        s = self._state
        meta: dict[str, Any] = {
            "current_state": s.current_state,
            "strategy_date_ist": s.strategy_date_ist,
            "weekday_allowed": s.weekday_allowed,
            "pair_found": s.pair_found,
            "breakout_ready": s.breakout_ready,
            "range_high": s.range_high,
            "range_low": s.range_low,
            "range_size": s.range_size,
            "pair_first_candle_open_time": s.pair_first_candle.get("open_time") if s.pair_first_candle else None,
            "pair_second_candle_open_time": s.pair_second_candle.get("open_time") if s.pair_second_candle else None,
            "virtual_in_trade": s.virtual_in_trade,
            "virtual_side": s.virtual_side,
            "virtual_entry": s.virtual_entry,
            "virtual_sl": s.virtual_sl,
            "virtual_tp": s.virtual_tp,
            "tp_hit": s.tp_hit,
            "sl_count": s.sl_count,
            "halted_for_day": s.halted_for_day,
            "no_trade_reason": s.no_trade_reason,
            "breakout_armed": s.breakout_armed,
            "recent_events": list(s.event_log)[-10:],
        }
        try:
            key = self._get_persistence_key(s.strategy_date_ist)
            self._store.upsert_component_status(
                component_name=key,
                status=s.current_state,
                last_update_ts=self._now_ms(),
                meta=meta,
            )
            self._logger.debug("persist_state: %s (%s)", s.current_state, key)
        except Exception as e:
            self._logger.warning("persist_state failed: %s", e)

    def load_state(self, date_ist: str | None = None) -> bool:
        """Load internal strategy state from StateStore."""
        try:
            # Use provided date, or internal target_date, or default to today's IST date
            effective_date = date_ist or self._target_date or datetime.now(self._tz).strftime("%Y-%m-%d")
            key = self._get_persistence_key(effective_date)
            
            row = self._store.get_component_status(key)
            if not row or "meta" not in row:
                return False
            
            meta = row["meta"]
            s = self._state
            s.current_state = meta.get("current_state", WAITING_FOR_NEW_DAY)
            s.strategy_date_ist = meta.get("strategy_date_ist", "")
            s.weekday_allowed = meta.get("weekday_allowed", False)
            s.pair_found = meta.get("pair_found", False)
            s.breakout_ready = meta.get("breakout_ready", False)
            s.range_high = meta.get("range_high")
            s.range_low = meta.get("range_low")
            s.range_size = meta.get("range_size")
            s.virtual_in_trade = meta.get("virtual_in_trade", False)
            s.virtual_side = meta.get("virtual_side", "")
            s.virtual_entry = meta.get("virtual_entry")
            s.virtual_sl = meta.get("virtual_sl")
            s.virtual_tp = meta.get("virtual_tp")
            s.tp_hit = meta.get("tp_hit", False)
            s.sl_count = meta.get("sl_count", 0)
            s.halted_for_day = meta.get("halted_for_day", False)
            s.session_ended = meta.get("session_ended", False)
            s.breakout_armed = meta.get("breakout_armed", False)
            s.no_trade_reason = meta.get("no_trade_reason", "")
            
            events = meta.get("recent_events", [])
            s.event_log.clear()
            s.event_log.extend(events)
            
            self._logger.info("load_state: recovered state for %s (armed=%s, in_trade=%s, sl=%s)", 
                             s.strategy_date_ist, s.breakout_armed, s.virtual_in_trade, s.sl_count)
            return True
        except Exception as e:
            self._logger.error("load_state failed: %s", e)
            return False

    def set_manual_range(self, high: float, low: float) -> None:
        """Manually override the trading range (for testing/bypass)."""
        self._state.range_high = self._round_price(high)
        self._state.range_low = self._round_price(low)
        if self._state.range_high is not None and self._state.range_low is not None:
            self._state.range_size = self._state.range_high - self._state.range_low
            self._state.pair_found = True
            self._state.breakout_ready = True
            
            # Full reset for clean test: clear trade state, halt, session flags
            self._state.virtual_in_trade = False
            self._state.virtual_side = None
            self._state.breakout_armed = True  # Auto-arm so breakout fires immediately
            self._state.halted_for_day = False  # Clear halt from previous SLs
            self._state.sl_count = 0  # Reset SL counter
            self._state.session_ended = False  # Clear session end flag
            self._state.current_state = RANGE_DEFINED
            self._state.weekday_allowed = True
            self._state.session_started = True
            
            # Set today's date to prevent reset_for_new_day from wiping the range
            from datetime import datetime
            today_ist = datetime.now(IST).strftime("%Y-%m-%d")
            self._state.strategy_date_ist = today_ist
            
            self._logger.info("MANUAL RANGE RESET & ACTIVATED (ARMED): high=%.2f, low=%.2f, size=%.2f (halt/session cleared, date=%s)", 
                             self._state.range_high, self._state.range_low, self._state.range_size, today_ist)




    def close(self) -> None:
        try:
            self._store.close()
        except Exception as e:
            self._logger.warning("close: %s", e)


# ---------------------------------------------------------------------------
# Debug / demo
# ---------------------------------------------------------------------------
def _make_5m_candle(open_time_ms: int, o: float, h: float, l: float, c: float) -> dict:
    return {
        "symbol": "BTC-USDC",
        "interval": "5m",
        "open_time": open_time_ms,
        "close_time": open_time_ms + 300_000,
        "open": o, "high": h, "low": l, "close": c,
        "tick_count": 0,
        "is_closed": True,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.getLogger("src.state_store").setLevel(logging.WARNING)

    # Simulate IST: 08:00 IST = 02:30 UTC. Use a fixed UTC base for 08:00 IST on a weekday.
    # 2025-03-10 02:30 UTC = 08:00 IST Monday
    base_utc = datetime(2025, 3, 10, 2, 30, 0, tzinfo=ZoneInfo("UTC"))
    base_ms = int(base_utc.timestamp() * 1000)

    engine = BreakoutStrategyEngine(symbol="BTC-USDC", db_path="data/bot_state.db")
    try:
        # 1) First candle 08:00-08:05 IST (no previous)
        c1 = _make_5m_candle(base_ms, 100.0, 101.0, 99.0, 100.5)  # green
        evt = engine.process_closed_5m_candle(c1)
        print("C1 (08:00-08:05 green):", evt)
        print("  summary:", engine.get_compact_summary())

        # 2) Second candle 08:05-08:10 (red) -> same color pair not opposite
        c2 = _make_5m_candle(base_ms + 300_000, 100.5, 101.0, 99.5, 99.5)  # red
        evt = engine.process_closed_5m_candle(c2)
        print("C2 (08:05-08:10 red):", evt)
        print("  summary:", engine.get_compact_summary())

        # 3) Third candle 08:10-08:15 (green) -> pair (c2,c3) = red then green = valid
        c3 = _make_5m_candle(base_ms + 600_000, 99.5, 100.0, 99.0, 99.8)  # green
        evt = engine.process_closed_5m_candle(c3)
        print("C3 (08:10-08:15 green):", evt)
        print("  summary:", engine.get_compact_summary())

        # 4) Breakout above range -> virtual long
        # range_high = max(101,100)=101, range_low = min(99.5,99)=99 -> range_size=2
        evt = engine.process_live_price(102.0, base_ms + 700_000)  # above 101
        print("Live 102 (breakout long):", evt)
        print("  summary:", engine.get_compact_summary())

        # 5) TP hit: long TP = range_high + 4*range_size = 101 + 8 = 109
        evt = engine.process_live_price(109.0, base_ms + 710_000)
        print("Live 109 (TP):", evt)
        print("  summary:", engine.get_compact_summary())

        engine.persist_state()
        print("State persisted.")

        # --- Second scenario: 3 SL hits then halt ---
        print("\n--- Scenario: 3 SL hits ---")
        engine2 = BreakoutStrategyEngine(symbol="BTC-USDC", db_path="data/bot_state.db")
        engine2.reset_for_new_day(base_ms)
        engine2._state.weekday_allowed = True
        engine2._state.session_started = True
        engine2._state.pair_found = True
        engine2._state.breakout_ready = True
        engine2._state.range_high = 100.0
        engine2._state.range_low = 98.0
        engine2._state.range_size = 2.0
        engine2._state.current_state = RANGE_DEFINED

        # Long entry then SL
        engine2.process_live_price(101.0, base_ms + 3600_000)
        evt = engine2.process_live_price(97.5, base_ms + 3700_000)  # below 98 = SL
        print("SL 1:", evt)
        evt = engine2.process_live_price(101.0, base_ms + 3800_000)
        evt = engine2.process_live_price(97.5, base_ms + 3900_000)
        print("SL 2:", evt)
        evt = engine2.process_live_price(101.0, base_ms + 4000_000)
        evt = engine2.process_live_price(97.5, base_ms + 4100_000)
        print("SL 3 (halt):", evt)
        print("  summary:", engine2.get_compact_summary())
        engine2.close()
    finally:
        engine.close()
    print("Done.")
