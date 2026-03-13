"""
Live trading integration runner for the Hyperliquid BTC-USDC 5m breakout strategy.

Connects WebSocket market data, CandleBuilder, BreakoutStrategyEngine, and HyperliquidClient.
Executes real orders on Hyperliquid based on strategy signals.
"""

from __future__ import annotations

import logging
import time
import os
import sys
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Add project root to sys.path
sys.path.append(os.getcwd())

from src.breakout_strategy import BreakoutStrategyEngine
from src.candle_builder import CandleBuilder
from src.hyperliquid_client import HyperliquidClient
from src.state_store import StateStore
from src.ws_marketdata import HyperliquidWSMarketData
from src.trade_logger import TradeLogger
from src.pnl_logger import PnLLogger
from src.bootstrap_history import HistoryBootstrapper

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")
RUNNER_NAME = "run_breakout_live"
DEFAULT_DB_PATH = "data/bot_state.db"
POLL_INTERVAL_SEC = 0.25
HEARTBEAT_INTERVAL_SEC = 5.0
EQUITY_REFRESH_INTERVAL_SEC = 60.0 # Fetch equity every minute

# TEST MODE: Set to True to override range manually
TEST_MODE = False
TEST_RANGE = {"high": 70200.0, "low": 70100.0}

# DATE TO TRADE: Set to "YYYY-MM-DD" to force a specific day, or None for auto-current
DATE_TO_TRADE = "2026-03-14"

def _setup_logging() -> logging.Logger:
    log = logging.getLogger("run_breakout_live")
    log.setLevel(logging.INFO)
    
    # Create console handler with a clean format
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s', datefmt='%H:%M:%S')
    ch.setFormatter(formatter)
    
    if not log.handlers:
        log.addHandler(ch)
    
    # Forcibly silence noisy sub-modules by clearing their handlers AND raising their level
    for noisy in ("hyperliquid_client", "ws_marketdata", "urllib3", "websocket"):
        noisy_log = logging.getLogger(noisy)
        noisy_log.handlers.clear()
        noisy_log.setLevel(logging.WARNING)
        noisy_log.propagate = False  # Don't bubble up to root
    
    # breakout_strategy can show INFO (for range logs etc.) but clear its DEBUG handler
    bs_log = logging.getLogger("breakout_strategy")
    bs_log.handlers.clear()
    bs_log.setLevel(logging.INFO)
    bs_log.addHandler(ch)  # Reuse our clean formatter
    bs_log.propagate = False
    
    return log

class BreakoutLiveRunner:
    def __init__(self, symbol: str = "BTC-USDC", db_path: str = DEFAULT_DB_PATH):
        self.symbol = symbol
        self.db_path = db_path
        self._logger = _setup_logging()
        
        self._store = StateStore(db_path=db_path)
        self._strategy = BreakoutStrategyEngine(symbol=symbol, db_path=db_path, target_date=DATE_TO_TRADE)
        self._candle_builder = CandleBuilder(symbol=symbol)
        self._ws = HyperliquidWSMarketData(symbol=symbol)
        self._client = HyperliquidClient()
        self._trade_logger = TradeLogger()
        
        self._running = False
        self._equity = 0.0
        self._last_equity_fetch = 0.0
        self._last_processed_5m_open_time = None
        self._pnl_logger = PnLLogger()
        
        # Heartbeat file
        self.heartbeat_file = "logs/heartbeat.txt"
        os.makedirs("logs", exist_ok=True)
        
        # Track current trade for PnL
        self._current_entry_price = None
        self._current_direction = None
        self._current_size = None
        
        # Stats
        self.ticks_processed = 0
        self.last_event_type = None

    def _update_equity(self):
        """Fetch current account equity from the Perp account."""
        now = time.time()
        if now - self._last_equity_fetch < EQUITY_REFRESH_INTERVAL_SEC:
            return
        
        try:
             summary = self._client.get_account_summary()
             self._equity = float(summary.get("equity", 0.0))
             self._last_equity_fetch = now
        except Exception as e:
             self._logger.warning("Failed to update equity: %s", e)

    def _handle_signal(self, result: dict[str, Any]):
        """Place real orders when a signal is emitted."""
        entry = result.get("entry")
        if isinstance(entry, dict):
            side = "buy" if entry.get("signal") == "long_entry" else "sell"
            direction = "long" if side == "buy" else "short"
            price = entry.get("price")
            size = entry.get("size")
            
            self._logger.info("!!! LIVE SIGNAL: %s @ %s Size: %s BTC !!!", side.upper(), price, size)
            
            try:
                # Place MARKET order for breakout entry
                resp = self._client.place_order(
                    symbol=self.symbol,
                    side=side,
                    order_type="market",
                    qty=size,
                    price=price
                )
                self._logger.info("Entry order placed successfully: %s", resp)
                
                # Track entry for PnL
                try:
                    fill = resp.get("response", {}).get("data", {}).get("statuses", [{}])[0]
                    filled_info = fill.get("filled", {})
                    self._current_entry_price = float(filled_info.get("avgPx", price))
                    self._current_size = float(filled_info.get("totalSz", size))
                except Exception:
                    self._current_entry_price = float(price)
                    self._current_size = float(size)
                self._current_direction = direction
                
                # Log to daily JSON
                self._trade_logger.log_event("trade_entry", {
                    "symbol": self.symbol,
                    "side": side,
                    "entry_price": self._current_entry_price,
                    "size": self._current_size,
                    "sl": entry.get("sl"),
                    "tp": entry.get("tp"),
                    "response": resp
                })

            except Exception as e:
                self._logger.error("Failed to place live entry order: %s", e)

        exit_evt = result.get("exit")
        if isinstance(exit_evt, dict):
             self._logger.info("!!! LIVE EXIT SIGNAL Triggered: %s !!!", exit_evt)
             
             exit_type = exit_evt.get("exit")
             if exit_type in ["market_close", "sl", "tp"]:
                  # If we were long, we sell to close. If short, we buy to close.
                  side = "sell" if exit_evt.get("side") == "long" else "buy"
                  exit_price = float(exit_evt.get("price", 0))
                  
                  try:
                       # Fetch actual position size from exchange
                       positions = self._client.get_positions()
                       relevant_pos = next((p for p in positions if p["symbol"] == self.symbol), None)
                       if relevant_pos:
                            size = abs(float(relevant_pos["size"]))
                       else:
                            size = self._current_size or 0.0002
                            self._logger.warning("No position found, using tracked size: %s", size)
                       
                       # Use the exit price from the strategy (NOT 0!)
                       # If no price available, fetch current market price
                       if not exit_price:
                           ticker = self._client.get_ticker(self.symbol)
                           exit_price = float(ticker.get("last_price", 0))
                       
                       self._logger.info("EXPLICIT EXIT (%s): Closing %s %s units at MARKET (ref price: %.2f)...", 
                                        exit_type.upper(), side.upper(), size, exit_price)
                       resp = self._client.place_order(
                            symbol=self.symbol,
                            side=side,
                            order_type="market",
                            qty=size,
                            price=exit_price,
                            reduce_only=True
                       )
                       self._logger.info("Market close response: %s", resp)
                       
                       # Calculate actual fill price for PnL
                       actual_exit_price = exit_price
                       try:
                           fill = resp.get("response", {}).get("data", {}).get("statuses", [{}])[0]
                           if "filled" in fill:
                               actual_exit_price = float(fill["filled"].get("avgPx", exit_price))
                       except Exception:
                           pass

                       # Log PnL to CSV
                       if self._current_entry_price and self._current_direction:
                           pnl_result = self._pnl_logger.log_trade(
                               symbol=self.symbol,
                               direction=self._current_direction,
                               entry_price=self._current_entry_price,
                               exit_price=actual_exit_price,
                               size_btc=size,
                               exit_reason=exit_type,
                           )
                           self._logger.info("PnL: $%.4f (%.4f%%) | %s",
                                           pnl_result["pnl_usd"], pnl_result["pnl_pct"], exit_type.upper())
                       
                       # Clear tracked trade
                       self._current_entry_price = None
                       self._current_direction = None
                       self._current_size = None

                       # Log to daily JSON
                       self._trade_logger.log_event("trade_exit", {
                           "symbol": self.symbol,
                           "side": side,
                           "exit_type": exit_type,
                           "price": actual_exit_price,
                           "size": size,
                           "response": resp
                       })
                  except Exception as e:
                       self._logger.error("Failed to execute market exit for %s: %s", exit_type, e)

    def _replay_today_candles(self):
        """Fetch today's candles from DB and feed to strategy + bootstrap candle builder."""
        now_ms = int(time.time() * 1000)
        today_date = DATE_TO_TRADE or datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d")
        
        self._logger.info("Replaying candles for date %s to catch up state...", today_date)
        
        # 1. Bootstrap CandleBuilder for both 1m and 5m so it doesn't re-emit them
        for interval in ("5m", "1m"):
            rows = self._store.get_recent_closed_candles(self.symbol, interval, limit=200)
            today_list = []
            for r in rows:
                r_date = datetime.fromtimestamp(r["open_time"] / 1000.0, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d")
                if r_date == today_date:
                    today_list.append(r)
            
            if not today_list:
                self._logger.info("No %s candles for today in DB. Attempting to fetch from API...", interval)
                try:
                    boot = HistoryBootstrapper(symbol=self.symbol, db_path=DEFAULT_DB_PATH)
                    # Fetch enough to cover the morning (720 min = 12 hours)
                    lookback = 720 if interval == "5m" else 120
                    fetched = boot.fetch_recent_candles(interval, lookback)
                    if fetched:
                        boot.persist_candles(fetched)
                        # Re-filter for today after persistence
                        for f in fetched:
                            f_date = datetime.fromtimestamp(f["open_time"] / 1000.0, tz=timezone.utc).astimezone(IST).strftime("%Y-%m-%d")
                            if f_date == today_date:
                                # Avoid duplicating if it was somehow in rows but filtered out weirdly (unlikely)
                                if not any(x["open_time"] == f["open_time"] for x in today_list):
                                    today_list.append(f)
                        self._logger.info("Fetched and persisted %d %s candles from API", len(fetched), interval)
                    boot.close()
                except Exception as e:
                    self._logger.warning("Failed to bootstrap %s candles from API: %s", interval, e)

            if today_list:
                today_list.sort(key=lambda x: x["open_time"])
                self._candle_builder.bootstrap_closed_candles(interval, today_list)
                self._logger.info("Bootstrapped %s with %d candles", interval, len(today_list))
                
                # 2. Feed 5m candles to strategy to define range
                if interval == "5m":
                    for c in today_list:
                        evt = self._strategy.process_closed_5m_candle(c)
                        if evt.get("pair_found") or evt.get("pair_already_found"):
                             summary = self._strategy.get_compact_summary()
                             if summary.get("pair_found"):
                                 self._logger.info("Replay range status: high=%s, low=%s", 
                                                  summary.get("range_high"), summary.get("range_low"))
                                 # Log range discovery to JSON
                                 # We log it every time it's "confirmed" in replay to ensure the session JSON has it
                                 self._trade_logger.log_event("range_identified", {
                                     "range_high": summary.get("range_high"),
                                     "range_low": summary.get("range_low"),
                                     "range_size": summary.get("range_size"),
                                     "replayed": True
                                 })
                                 # Once we've logged it once during replay, we can stop logging duplicates from replay
                                 break 

    def start(self):
        trade_date_label = DATE_TO_TRADE or "CURRENT DATE"
        self._logger.info("Starting LIVE runner for %s (Target Day: %s)", self.symbol, trade_date_label)
        
        # 0. Set leverage to 1x for safety
        try:
            self._logger.info("Setting leverage to 1x for %s...", self.symbol)
            self._client.set_leverage(1, self.symbol, is_cross=True)
        except Exception as e:
            self._logger.warning("Failed to set leverage: %s. Continuing anyway...", e)

        # 1. Recover last known state from DB
        self._strategy.load_state()
        
        # 2. Replay history to catch up to the current range
        self._replay_today_candles()

        # 3. Apply Manual Test Range if TEST_MODE is active
        if TEST_MODE:
             self._logger.info("!!! TEST MODE ACTIVE !!! Manually setting range: %s", TEST_RANGE)
             self._strategy.set_manual_range(TEST_RANGE["high"], TEST_RANGE["low"])
             self._trade_logger.log_event("manual_range_override", {
                 "test_mode": True,
                 "range_high": TEST_RANGE["high"],
                 "range_low": TEST_RANGE["low"]
             })

        # 4. Explicitly log recovered state if range is already known at start
        state = self._strategy.get_state_snapshot()
        if state.get("pair_found"):
            self._trade_logger.log_event("range_recovered_at_startup", {
                "range_high": state.get("range_high"),
                "range_low": state.get("range_low"),
                "range_size": state.get("range_size"),
                "armed": state.get("breakout_armed")
            })
        
        # 4. Start live data
        self._update_equity()
        self._ws.start()
        self._running = True
        
        try:
            while self._running:
                self._update_equity()
                snapshot = self._ws.get_latest_snapshot()
                
                # Extract price
                price = snapshot.get("last_price") or snapshot.get("mid")
                if not price:
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                
                ts_ms = snapshot.get("exchange_ts") or int(time.time() * 1000)
                
                # Update candles
                cb_res = self._candle_builder.process_tick(price, ts_ms)
                new_5m = cb_res.get("new_5m_closed")
                if new_5m:
                    self._store.save_closed_candle(new_5m)
                    evt = self._strategy.process_closed_5m_candle(new_5m)
                    if evt.get("pair_found"):
                         # Log live range discovery
                         self._trade_logger.log_event("range_identified", {
                                "range_high": evt.get("range_high"),
                                "range_low": evt.get("range_low"),
                                "range_size": evt.get("range_size"),
                                "replayed": False
                         })
                
                # Process strategy
                res = self._strategy.process_live_price(price, ts_ms, equity=self._equity)
                
                # Heartbeat: log price every ~5 seconds
                self.ticks_processed += 1
                if self.ticks_processed % 25 == 1:  # ~every 5 seconds at 200ms poll
                    state = self._strategy.get_state_snapshot()
                    armed = "ARMED" if state.get("breakout_armed") else "waiting"
                    in_trade = state.get("virtual_side", "none") if state.get("virtual_in_trade") else "no"
                    range_low = state.get("range_low") or 0
                    range_high = state.get("range_high") or 0
                    self._logger.info(
                        "Heartbeat | Price: $%.2f | Range: [%.0f - %.0f] | Armed: %s | InTrade: %s | Eq: $%.2f",
                        float(price), range_low, range_high,
                        armed, in_trade, self._equity
                    )
                    
                    # Update heartbeat file (overwrite single line)
                    try:
                        with open(self.heartbeat_file, "w") as f:
                            timestamp = datetime.now(IST).strftime("%H:%M:%S")
                            f.write(f"[{timestamp}] BTC: ${float(price):.2f} | Range: [{range_low:.0f}-{range_high:.0f}] | Status: {state.get('current_state')} | Armed: {armed} | Side: {in_trade} | SLs: {state.get('sl_count')}/3 | TP: {'Hit' if state.get('tp_hit') else 'None'} | Halted: {'Yes' if state.get('halted_for_day') else 'No'} | Eq: ${self._equity:.2f}\n")
                    except Exception as e:
                        self._logger.warning("Failed to update heartbeat file: %s", e)
                
                if res.get("entry") or res.get("exit"):
                    self._handle_signal(res)
                    self._strategy.persist_state()
                
                # Auto-shutdown: Check if session ended and we are not in trade
                if res.get("session_ended") and not self._strategy.get_state_snapshot().get("virtual_in_trade"):
                     self._logger.info("Session ended and any positions closed. Shutting down runner.")
                     self._running = False
                     break

                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            self._logger.info("Stopping...")
        finally:
            self._ws.stop()
            self._store.close()

if __name__ == "__main__":
    runner = BreakoutLiveRunner()
    runner.start()
