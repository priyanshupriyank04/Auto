r"""
WebSocket market data client for Hyperliquid (MAINNET).

This module provides:
  - HyperliquidWSMarketData class for live market data over WebSocket
  - Subscribe to BTC (or configured symbol) l2Book, trades, bbo, and optional activeAssetCtx
  - Normalized latest snapshot in memory (bid, ask, last_price, mark_price, oracle_price, etc.)
  - Automatic reconnect with exponential backoff + jitter
  - Thread-safe snapshot and status access for consumers (e.g. candle_builder.py)

This file is NOT for:
  - Trading strategy logic
  - Candle building / aggregation
  - Order placement
  - REST requests (except optional symbol normalization behavior aligned with hyperliquid_client)
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from typing import Any

import websocket

# ---------------------------------------------------------------------------
# Constants — TODO: adjust if Hyperliquid changes WS URL or channel names
# ---------------------------------------------------------------------------
DEFAULT_WS_URL_MAINNET = "wss://api.hyperliquid.xyz/ws"
ENV_WS_URL_KEY = "HL_WS_URL"

# Channel names in subscription payload and in server response "channel" field.
# TODO: confirm exact strings from live API if subscription fails.
CHANNEL_ALL_MIDS = "allMids"
CHANNEL_L2_BOOK = "l2Book"
CHANNEL_TRADES = "trades"
CHANNEL_BBO = "bbo"
CHANNEL_ACTIVE_ASSET_CTX = "activeAssetCtx"
CHANNEL_SUBSCRIPTION_RESPONSE = "subscriptionResponse"

# Reconnect settings
RECONNECT_BASE_DELAY_SEC = 1.0
RECONNECT_MAX_DELAY_SEC = 60.0
RECONNECT_MAX_ATTEMPTS = 0  # 0 = no upper bound (retry until stop)


def _default_snapshot_dict(symbol: str) -> dict[str, Any]:
    """Return a default normalized snapshot with all expected keys; values None or placeholder."""
    return {
        "symbol": symbol,
        "last_price": None,
        "bid": None,
        "ask": None,
        "mark_price": None,
        "oracle_price": None,
        "exchange_ts": None,
        "local_ts": None,
        "status": "disconnected",
        "raw": None,
    }


class HyperliquidWSMarketData:
    """
    WebSocket client for Hyperliquid live market data.
    Maintains a single normalized snapshot for the configured symbol (e.g. BTC-USDC).
    Thread-safe; suitable for consumption by candle_builder or other modules.
    """

    def __init__(self, symbol: str = "BTC-USDC", ws_url: str | None = None) -> None:
        """
        Initialize WebSocket market data client.

        Args:
            symbol: Canonical symbol in our system (e.g. BTC-USDC).
            ws_url: WebSocket URL. If None, uses HL_WS_URL env or mainnet default.
        """
        self.symbol = symbol.strip() or "BTC-USDC"
        self._ws_url = (
            (ws_url or "").strip()
            or (os.environ.get(ENV_WS_URL_KEY) or "").strip()
            or DEFAULT_WS_URL_MAINNET
        )
        self._logger = self._init_logger()

        # Connection state
        self._ws: websocket.WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._connected = False
        self._status = "initialized"

        # Latest normalized snapshot (thread-safe via lock)
        self._snapshot = _default_snapshot_dict(self.symbol)
        self._lock = threading.RLock()

        # Control
        self._stop_event = threading.Event()
        self._listener_started = threading.Event()

        # Reconnect
        self._reconnect_attempts = 0
        self._reconnect_base_delay = RECONNECT_BASE_DELAY_SEC
        self._reconnect_max_delay = RECONNECT_MAX_DELAY_SEC
        self._reconnect_max_attempts = RECONNECT_MAX_ATTEMPTS
        self._last_close_reason: str | None = None
        self._expecting_close = False

        # Counters and freshness
        self._messages_received = 0
        self._last_message_local_ts: float | None = None

    def _init_logger(self) -> logging.Logger:
        """Configure and return module logger."""
        log = logging.getLogger("ws_marketdata")
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

    # -------------------------------------------------------------------------
    # Symbol normalization (aligned with hyperliquid_client; no REST)
    # -------------------------------------------------------------------------
    def _normalize_symbol_in(self, symbol: str) -> str:
        """
        Convert our canonical symbol (e.g. BTC-USDC) to exchange format.
        Hyperliquid perp uses coin name like 'BTC'.
        """
        if not symbol:
            return symbol
        if "-" in symbol:
            return symbol.split("-")[0].strip()
        return symbol.strip()

    def _normalize_symbol_out(self, exchange_symbol: str) -> str:
        """Convert exchange symbol (e.g. BTC) to our canonical form (e.g. BTC-USDC)."""
        if not exchange_symbol:
            return exchange_symbol
        s = exchange_symbol.strip()
        if "-" in s:
            return s
        return f"{s}-USDC"

    @staticmethod
    def _now_ms() -> int:
        """Current time in milliseconds since epoch."""
        return int(time.time() * 1000)

    @staticmethod
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

    def _default_snapshot(self) -> dict[str, Any]:
        """Return a fresh default snapshot for our symbol."""
        return _default_snapshot_dict(self.symbol)

    # -------------------------------------------------------------------------
    # Subscription payloads — TODO: adjust channel types/keys if API differs
    # -------------------------------------------------------------------------
    def _build_subscription_payloads(self) -> list[dict[str, Any]]:
        """
        Build list of subscription messages for our symbol.
        Hyperliquid expects: { "method": "subscribe", "subscription": { "type": "...", ... } }
        """
        coin = self._normalize_symbol_in(self.symbol)
        # TODO: confirm exact "type" and optional params (e.g. dex, nSigFigs) per Hyperliquid docs.
        return [
            {"method": "subscribe", "subscription": {"type": CHANNEL_L2_BOOK, "coin": coin}},
            {"method": "subscribe", "subscription": {"type": CHANNEL_TRADES, "coin": coin}},
            {"method": "subscribe", "subscription": {"type": CHANNEL_BBO, "coin": coin}},
            {"method": "subscribe", "subscription": {"type": CHANNEL_ACTIVE_ASSET_CTX, "coin": coin}},
            # allMids is global; we still use it to get mid for our coin if others lack data
            {"method": "subscribe", "subscription": {"type": CHANNEL_ALL_MIDS}},
        ]

    def subscribe(self) -> None:
        """
        Send subscription payload(s) for the configured symbol.
        Call after connection is opened. TODO: adjust if channel names or payload shape change.
        """
        if not self._ws:
            self._logger.warning("subscribe called but websocket is None")
            return
        payloads = self._build_subscription_payloads()
        for payload in payloads:
            try:
                raw = json.dumps(payload)
                self._ws.send(raw)
                self._logger.info(
                    "subscription_sent type=%s",
                    payload.get("subscription", {}).get("type", "?"),
                )
            except Exception as e:
                self._logger.error(
                    "subscribe_failed payload=%s error=%s",
                    payload,
                    type(e).__name__,
                    exc_info=True,
                )

    # -------------------------------------------------------------------------
    # Message handling and extraction
    # -------------------------------------------------------------------------
    def _extract_best_bid_ask(self, data: dict[str, Any]) -> tuple[float | None, float | None]:
        """
        Extract best bid and ask from l2Book (levels) or bbo message.
        l2Book: data.levels = [bids, asks], each list of {px, sz, n}.
        bbo: data.bbo = [bid_level | null, ask_level | null], level = {px, sz, n}.
        """
        if not isinstance(data, dict):
            return None, None
        # bbo channel
        bbo = data.get("bbo")
        if isinstance(bbo, list) and len(bbo) >= 2:
            bid_lev, ask_lev = bbo[0], bbo[1]
            bid = None
            ask = None
            if isinstance(bid_lev, dict) and bid_lev.get("px") is not None:
                bid = self._safe_float(bid_lev.get("px"))
            if isinstance(ask_lev, dict) and ask_lev.get("px") is not None:
                ask = self._safe_float(ask_lev.get("px"))
            return bid, ask
        # l2Book channel: levels = [bids, asks]
        levels = data.get("levels")
        if isinstance(levels, list) and len(levels) >= 2:
            bids, asks = levels[0], levels[1]
            bid = None
            ask = None
            if isinstance(bids, list) and bids and isinstance(bids[0], dict):
                bid = self._safe_float(bids[0].get("px"))
            if isinstance(asks, list) and asks and isinstance(asks[0], dict):
                ask = self._safe_float(asks[0].get("px"))
            return bid, ask
        return None, None

    def _extract_last_price(self, data: Any) -> float | None:
        """
        Extract last price from trades message.
        data can be single WsTrade or list of WsTrade; we use the last one.
        """
        if isinstance(data, dict) and "px" in data:
            return self._safe_float(data.get("px"))
        if isinstance(data, list) and data:
            last_trade = data[-1]
            if isinstance(last_trade, dict):
                return self._safe_float(last_trade.get("px"))
        return None

    def _extract_mark_or_oracle(self, data: dict[str, Any]) -> tuple[float | None, float | None]:
        """
        Extract mark and oracle from activeAssetCtx (PerpsAssetCtx: markPx, oraclePx).
        """
        if not isinstance(data, dict):
            return None, None
        ctx = data.get("ctx")
        if not isinstance(ctx, dict):
            return None, None
        mark = self._safe_float(ctx.get("markPx"))
        oracle = self._safe_float(ctx.get("oraclePx"))
        return mark, oracle

    def _handle_message(self, channel: str, data: Any) -> None:
        """
        Update snapshot from a single channel message.
        Channel names match subscription types: l2Book, trades, bbo, activeAssetCtx, allMids.
        """
        coin = self._normalize_symbol_in(self.symbol)
        with self._lock:
            snap = self._snapshot
            exchange_ts: int | None = None
            if isinstance(data, dict) and "time" in data:
                t = data.get("time")
                if isinstance(t, (int, float)):
                    exchange_ts = int(t)

            if channel == CHANNEL_L2_BOOK or channel == CHANNEL_BBO:
                bid, ask = self._extract_best_bid_ask(data)
                if bid is not None:
                    snap["bid"] = str(bid)
                if ask is not None:
                    snap["ask"] = str(ask)
                if exchange_ts is not None:
                    snap["exchange_ts"] = exchange_ts

            elif channel == CHANNEL_TRADES:
                last_px = self._extract_last_price(data)
                if last_px is not None:
                    snap["last_price"] = str(last_px)
                if isinstance(data, dict) and "time" in data:
                    snap["exchange_ts"] = exchange_ts
                elif isinstance(data, list) and data and isinstance(data[-1], dict):
                    t = data[-1].get("time")
                    if t is not None:
                        snap["exchange_ts"] = int(t)

            elif channel == CHANNEL_ACTIVE_ASSET_CTX:
                mark, oracle = self._extract_mark_or_oracle(data)
                if mark is not None:
                    snap["mark_price"] = str(mark)
                if oracle is not None:
                    snap["oracle_price"] = str(oracle)

            elif channel == CHANNEL_ALL_MIDS:
                mids = data.get("mids") if isinstance(data, dict) else None
                if isinstance(mids, dict) and coin in mids:
                    mid = self._safe_float(mids.get(coin))
                    if mid is not None:
                        if snap.get("last_price") is None:
                            snap["last_price"] = str(mid)
                        if snap.get("bid") is None:
                            snap["bid"] = str(mid)
                        if snap.get("ask") is None:
                            snap["ask"] = str(mid)

            # Keep raw only for latest message (debugging); avoid storing huge payloads
            if isinstance(data, dict) and len(json.dumps(data)) < 2000:
                snap["raw"] = data
            else:
                snap["raw"] = {"_channel": channel, "_preview": "large payload"}

            snap["local_ts"] = self._now_ms()
            snap["status"] = "connected"
            self._last_message_local_ts = time.time()
            self._messages_received += 1
            if self._messages_received % 100 == 1:
                self._logger.debug(
                    "message_count count=%s channel=%s",
                    self._messages_received,
                    channel,
                )

    def _on_ws_message(self, _ws_app: Any, message: str | bytes) -> None:
        """WebSocket on_message callback: parse and dispatch to _handle_message."""
        try:
            raw = message.decode("utf-8") if isinstance(message, bytes) else message
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._logger.warning("parse_failure error=%s raw_preview=%s", e, str(message)[:200])
            return
        if not isinstance(obj, dict):
            self._logger.debug("ignore_non_dict_message")
            return
        channel = obj.get("channel")
        data = obj.get("data")
        if channel == CHANNEL_SUBSCRIPTION_RESPONSE:
            self._logger.info("subscription_ack data=%s", data)
            return
        if channel and data is not None:
            self._handle_message(channel, data)
        else:
            self._logger.debug("message_missing_channel_or_data keys=%s", list(obj.keys()))

    def _on_ws_error(self, _ws_app: Any, error: Exception) -> None:
        """WebSocket on_error callback."""
        self._logger.error("ws_error error=%s", type(error).__name__, exc_info=error)

    def _on_ws_close(self, _ws_app: Any, status_code: int | None, close_msg: str | None) -> None:
        """WebSocket on_close callback."""
        self._connected = False
        self._last_close_reason = f"code={status_code} msg={close_msg!r}"
        self._logger.info(
            "ws_close reason=%s expecting_close=%s",
            self._last_close_reason,
            self._expecting_close,
        )
        with self._lock:
            self._snapshot["status"] = "disconnected"

    def _on_ws_open(self, _ws_app: Any) -> None:
        """WebSocket on_open callback: mark connected and send subscriptions."""
        self._connected = True
        self._reconnect_attempts = 0
        self._status = "connected"
        self._logger.info("ws_open url=%s", self._ws_url)
        self.subscribe()

    # -------------------------------------------------------------------------
    # Connection and background loop
    # -------------------------------------------------------------------------
    def connect(self) -> None:
        """
        Open WebSocket connection and start internal listener thread.
        Does not block; run_forever runs in a daemon thread.
        """
        if self._stop_event.is_set():
            self._logger.warning("connect ignored: stop already set")
            return
        self._expecting_close = False
        self._status = "connecting"
        self._logger.info("connection_start url=%s symbol=%s", self._ws_url, self.symbol)
        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        self._ws_thread = threading.Thread(target=self._run_forever, daemon=True)
        self._ws_thread.start()
        self._listener_started.set()

    def _run_forever(self) -> None:
        """Run ws.run_forever() in this thread (blocking)."""
        if self._ws:
            try:
                self._ws.run_forever()
            except Exception as e:
                self._logger.error("run_forever_error error=%s", e, exc_info=True)
        self._listener_started.set()

    def disconnect(self) -> None:
        """Cleanly close WebSocket and update status."""
        self._expecting_close = True
        self._status = "disconnecting"
        if self._ws:
            try:
                self._ws.close()
            except Exception as e:
                self._logger.debug("disconnect_close_error %s", e)
            self._ws = None
        self._connected = False
        self._status = "disconnected"
        with self._lock:
            self._snapshot["status"] = "disconnected"
        self._logger.info("disconnect_done")

    def start(self) -> None:
        """Start background market data loop: connect and reconnect on failure."""
        self._stop_event.clear()
        self._listener_started.clear()
        self.connect()
        # Start reconnection loop in a separate thread
        reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
        reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        """
        Periodically check connection; if disconnected and not stopped, reconnect with backoff.
        Also acts as a Watchdog: force-reconnects if the stream goes totally silent for 15 seconds.
        """
        while not self._stop_event.is_set():
            time.sleep(2.0)
            if self._stop_event.is_set():
                break
            
            # Watchdog: Kill zombie connections that haven't received packets in 15 seconds
            if self._connected and self._last_message_local_ts is not None:
                if time.time() - self._last_message_local_ts > 15.0:
                    self._logger.error("WATCHDOG: Stream dead for 15s! Force closing zombie connection...")
                    if self._ws:
                        self._ws.close()
                    # It will loop around, _connected will be False, and it will reconnect naturally.
                    continue
                    
            if self._connected:
                continue
            if self._expecting_close:
                continue
            self._reconnect_attempts += 1
            if (
                self._reconnect_max_attempts > 0
                and self._reconnect_attempts > self._reconnect_max_attempts
            ):
                self._logger.warning(
                    "reconnect_max_attempts_reached attempts=%s",
                    self._reconnect_attempts,
                )
                continue
            delay = min(
                self._reconnect_base_delay * (2 ** (self._reconnect_attempts - 1))
                + random.uniform(0, 1.0),
                self._reconnect_max_delay,
            )
            self._logger.warning(
                "reconnect_attempt attempts=%s delay_sec=%.2f reason=%s",
                self._reconnect_attempts,
                delay,
                self._last_close_reason or "unknown",
            )
            time.sleep(delay)
            if self._stop_event.is_set():
                break
            self.connect()

    def stop(self) -> None:
        """Stop background loop, disconnect WebSocket, and prevent further reconnect attempts."""
        self._logger.info("stop requested")
        self._stop_event.set()
        self.disconnect()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5.0)
        self._status = "stopped"
        self._logger.info("stop done")

    # -------------------------------------------------------------------------
    # Public read API
    # -------------------------------------------------------------------------
    def get_latest_snapshot(self) -> dict[str, Any]:
        """Return a copy of the latest normalized market snapshot."""
        with self._lock:
            return dict(self._snapshot)

    def is_data_fresh(self, max_age_seconds: float = 2.0) -> bool:
        """
        Return True if latest data is fresh enough based on local receive time.
        """
        if self._last_message_local_ts is None:
            return False
        return (time.time() - self._last_message_local_ts) <= max_age_seconds

    def get_connection_status(self) -> dict[str, Any]:
        """Return connection and stats info for debugging."""
        with self._lock:
            return {
                "connected": self._connected,
                "status": self._status,
                "reconnect_attempts": self._reconnect_attempts,
                "messages_received": self._messages_received,
                "last_message_local_ts": self._last_message_local_ts,
                "symbol": self.symbol,
                "ws_url": self._ws_url,
            }


# ---------------------------------------------------------------------------
# Debug entrypoint (no orders, run ~30s then stop)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("ws_marketdata")
    logger.setLevel(logging.DEBUG)

    print("HyperliquidWSMarketData debug (market data only, no orders)")
    md = HyperliquidWSMarketData(symbol="BTC-USDC")
    try:
        md.start()
        deadline = time.time() + 30.0
        while time.time() < deadline:
            time.sleep(3.0)
            status = md.get_connection_status()
            print("status:", status)
            snap = md.get_latest_snapshot()
            print("snapshot:", {k: v for k, v in snap.items() if k != "raw"})
            print("fresh:", md.is_data_fresh(2.0))
        md.stop()
        print("stopped cleanly")
    except Exception as e:
        print(f"error: {e}")
        md.stop()
        raise
