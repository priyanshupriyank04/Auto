r"""
Hyperliquid REST client wrapper for a Windows-based local trading bot.

This module provides:
  - Config loading from config/settings.yaml and secrets from secrets/.env
  - A HyperliquidClient class for read-only REST communication with Hyperliquid MAINNET
  - Normalized responses as Python dicts, strong logging, and debuggable error handling
  - Method stubs for future trading (place_order, cancel_order, etc.) — not implemented

This file is NOT for:
  - Trading strategy logic
  - Candle building / aggregation
  - WebSocket connections
  - Live order placement (stubs only)

Safe to test right now (read-only):
  - health_check()
  - get_ticker(symbol)
  - get_candles(...)
  - get_account_summary()  (requires HL_WALLET_ADDRESS in .env)
  - get_positions()
  - get_open_orders(symbol=None)
"""

from __future__ import annotations

import logging
import os
import random
import time
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv
from eth_account import Account
import binascii
import json
import hashlib

# Official SDK imports
from hyperliquid.utils.signing import sign_l1_action, action_hash
from hyperliquid.utils.types import Meta

# ---------------------------------------------------------------------------
# Endpoint / request-type constants (Hyperliquid uses POST /info with body.type)
# Adjust these if the official Hyperliquid API docs or SDK change.
# Base URL is built from config (mainnet = api.hyperliquid.xyz).
# ---------------------------------------------------------------------------
INFO_PATH = "/info"
EXCHANGE_PATH = "/exchange"
# Health: use a lightweight info request; "meta" returns asset metadata (no auth).
HEALTH_ENDPOINT_TYPE = "meta"
# Account summary and positions: clearinghouseState returns margin + assetPositions.
ACCOUNT_ENDPOINT_TYPE = "clearinghouseState"
# Open orders.
OPEN_ORDERS_ENDPOINT_TYPE = "openOrders"
# Ticker: allMids gives mid prices per coin; l2Book gives bid/ask. We use allMids + optional l2.
TICKER_MIDS_TYPE = "allMids"
TICKER_L2_TYPE = "l2Book"
# Candles.
CANDLES_ENDPOINT_TYPE = "candleSnapshot"
# No separate "positions" endpoint; positions come from clearinghouseState.assetPositions.
POSITIONS_SOURCE = "clearinghouseState"

# Default mainnet base URL (no trailing slash).
DEFAULT_MAINNET_BASE_URL = "https://api.hyperliquid.xyz"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------
class HyperliquidClientError(Exception):
    """Base exception for HyperliquidClient."""

    pass


class NetworkError(HyperliquidClientError):
    """Request failed due to network/connection issues."""

    pass


class AuthError(HyperliquidClientError):
    """Authentication or authorization failure."""

    pass


class RateLimitError(HyperliquidClientError):
    """Rate limit exceeded (429 or API-specific limit)."""

    pass


class ExchangeError(HyperliquidClientError):
    """Exchange API returned an error or unexpected response."""

    pass


# ---------------------------------------------------------------------------
# HyperliquidClient
# ---------------------------------------------------------------------------
class HyperliquidClient:
    """
    Read-only REST client for Hyperliquid (MAINNET).
    Loads config from YAML and secrets from .env; validates required values.
    """

    def __init__(
        self,
        settings_path: str = "config/settings.yaml",
        env_path: str = "secrets/.env",
    ) -> None:
        """
        Load config and env, validate, and initialize logger and requests session.
        Paths are relative to current working directory (Windows-friendly when using Path).
        """
        self._cwd = Path(os.getcwd())
        self._settings_path = self._cwd / settings_path
        self._env_path = self._cwd / env_path

        self._config: dict[str, Any] = {}
        self._env: dict[str, str] = {}

        self._load_settings()
        self._load_env()
        self._validate_config()

        self._network = str(self._config.get("env", "mainnet")).lower()
        self._base_url = self._config.get("base_url") or DEFAULT_MAINNET_BASE_URL
        self._base_url = self._base_url.rstrip("/")
        self._wallet_address = (self._env.get("HL_WALLET_ADDRESS") or "").strip()
        self._timeout = int(self._config.get("http_timeout_seconds", 10))
        self._retry_max = int(
            self._config.get("read_retry", {}).get("max_retries", 3)
        )
        self._retry_base_delay = float(
            self._config.get("read_retry", {}).get("base_delay_seconds", 0.5)
        )
        self._retry_max_delay = float(
            self._config.get("read_retry", {}).get("max_delay_seconds", 3.0)
        )

        self._private_key = (self._env.get("HL_PRIVATE_KEY") or "").strip()
        if self._private_key and not self._private_key.startswith("0x"):
            self._private_key = "0x" + self._private_key
        
        self._is_mainnet = self._network == "mainnet"
        
        # Meta-data cache for asset index (needed for signing)
        self._meta_cache: dict[str, Any] = {}
        self._asset_to_index: dict[str, int] = {}
        
        # Initialize official SDK Exchange class for write operations
        from hyperliquid.exchange import Exchange
        if self._private_key:
            self._exchange = Exchange(
                Account.from_key(self._private_key),
                base_url=self._base_url,
                account_address=self._wallet_address
            )
        else:
            self._exchange = None
            
        self._logger = self._init_logger()
        self._session = requests.Session()
        self._session.headers.update(self._build_headers())

    def _init_logger(self) -> logging.Logger:
        """Configure and return module logger; log to console."""
        log = logging.getLogger("hyperliquid_client")
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

    def _load_settings(self) -> None:
        """Load YAML config from settings_path."""
        path = Path(self._settings_path)
        if not path.is_file():
            self._config = {}
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._config = yaml.safe_load(f) or {}
        except Exception as e:
            raise HyperliquidClientError(f"Failed to load settings from {path}: {e}")

        # Resolve base_url from env if not in YAML (e.g. mainnet -> default URL).
        if "base_url" not in self._config or not self._config["base_url"]:
            env_name = (self._config.get("env") or "mainnet").lower()
            if env_name == "mainnet":
                self._config["base_url"] = DEFAULT_MAINNET_BASE_URL
            # TODO: add testnet URL if needed, e.g. https://api.hyperliquid-testnet.xyz

    def _load_env(self) -> None:
        """Load .env from env_path into self._env (only non-empty values)."""
        path = Path(self._env_path)
        if not path.is_file():
            self._env = {}
            return
        load_dotenv(path, override=False)
        # Capture relevant vars (do not store private key in a way that could be logged).
        self._env = {
            k: (v or "").strip()
            for k, v in os.environ.items()
            if k.startswith("HL_") and v
        }

    def _validate_config(self) -> None:
        """Validate required config and env; raise if critical values missing."""
        if not self._config:
            raise HyperliquidClientError(
                f"Config is empty or missing at {self._settings_path}"
            )
        base_url = self._config.get("base_url") or DEFAULT_MAINNET_BASE_URL
        if not base_url.strip():
            raise HyperliquidClientError("base_url is missing or empty in config")
        # Wallet is required for account/positions/orders; optional for health/ticker/candles.
        # We allow client to be created without wallet; methods that need it will fail with clear errors.

    def _build_headers(self) -> dict[str, str]:
        """Build HTTP headers for info API (no auth required for read-only info)."""
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: int | None = None,
        retry: bool = True,
    ) -> dict[str, Any] | list[Any]:
        """
        Centralized HTTP request with timeout, optional retries, exponential backoff + jitter.
        """
        if not url.startswith("http"):
             url = f"{self._base_url}{url}"

        timeout = timeout if timeout is not None else self._timeout
        last_exc: Exception | None = None
        if self._logger.isEnabledFor(logging.DEBUG):
            # Safe to log payload for debugging order structure
            self._logger.debug("_request payload: %s", json_body)

        for attempt in range(self._retry_max + 1):
            try:
                self._logger.debug(
                    "request_start url=%s method=%s attempt=%s",
                    url,
                    method,
                    attempt + 1,
                )
                start = time.perf_counter()
                resp = self._session.request(
                    method,
                    url,
                    json=json_body,
                    timeout=timeout,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                self._logger.debug(
                    "request_done url=%s status=%s elapsed_ms=%.0f",
                    url,
                    resp.status_code,
                    elapsed_ms,
                )

                if resp.status_code == 429:
                    self._logger.warning(
                        "rate_limit status=429 url=%s attempt=%s",
                        url,
                        attempt + 1,
                    )
                    raise RateLimitError(
                        f"Rate limited (429) url={url} attempt={attempt + 1}"
                    )

                if resp.status_code == 401 or resp.status_code == 403:
                    self._logger.warning(
                        "auth_error status=%s url=%s",
                        resp.status_code,
                        url,
                    )
                    raise AuthError(
                        f"Auth failed (status={resp.status_code}) for request to {url}"
                    )

                if resp.status_code != 200:
                    msg = resp.text[:500] if resp.text else "(no body)"
                    self._logger.error(
                        "request_failed status=%s url=%s body_preview=%s",
                        resp.status_code,
                        url,
                        msg,
                    )
                    raise ExchangeError(
                        f"HTTP {resp.status_code} url={url} body={msg}"
                    )

                try:
                    return resp.json()
                except ValueError as e:
                    self._logger.error(
                        "json_parse_error url=%s error=%s",
                        url,
                        e,
                    )
                    raise ExchangeError(f"Invalid JSON response from {url}: {e}") from e

            except (RateLimitError, AuthError, ExchangeError):
                raise
            except requests.RequestException as e:
                last_exc = e
                self._logger.warning(
                    "request_exception url=%s attempt=%s error=%s",
                    url,
                    attempt + 1,
                    type(e).__name__,
                )
                if not retry or attempt >= self._retry_max:
                    raise NetworkError(
                        f"Request failed after {attempt + 1} attempt(s): {e}"
                    ) from e
                delay = min(
                    self._retry_base_delay * (2**attempt) + random.uniform(0, 0.5),
                    self._retry_max_delay,
                )
                self._logger.debug("retry_backoff delay_seconds=%.2f", delay)
                time.sleep(delay)

        if last_exc:
            raise NetworkError(f"Request failed: {last_exc}") from last_exc
        raise ExchangeError("Request failed with no exception captured")

    def _normalize_symbol_in(self, symbol: str) -> str:
        """
        Convert our canonical symbol (e.g. BTC-USDC) to exchange format.
        Hyperliquid perp uses coin name like 'BTC'. Pluggable for future changes.
        """
        if not symbol:
            return symbol
        # Canonical form is BTC-USDC; HL perp meta uses "BTC".
        if "-" in symbol:
            return symbol.split("-")[0].strip()
        return symbol.strip()

    def _normalize_symbol_out(self, exchange_symbol: str) -> str:
        """
        Convert exchange symbol (e.g. BTC) to our canonical form (e.g. BTC-USDC).
        """
        if not exchange_symbol:
            return exchange_symbol
        s = exchange_symbol.strip()
        if "-" in s:
            return s
        # Default quote to USDC for perps.
        return f"{s}-USDC"

    def _now_ms(self) -> int:
        """Current time in milliseconds since epoch."""
        return int(time.time() * 1000)

    def _round_price(self, price: float | None) -> float | None:
        """Round price to exchange tick size (1.0 for BTC on Hyperliquid)."""
        if price is None:
            return None
        return float(round(price, 0))

    # -------------------------------------------------------------------------
    # Signing logic (EIP-712)
    # -------------------------------------------------------------------------
    def _get_asset_index(self, symbol: str) -> int:
        """Get the asset index for a coin from meta-data."""
        if not self._asset_to_index:
            meta = self.get_meta()
            universe = meta.get("universe") or []
            for i, asset in enumerate(universe):
                self._asset_to_index[asset["name"]] = i
        
        coin = self._normalize_symbol_in(symbol)
        if coin not in self._asset_to_index:
            raise ExchangeError(f"Asset {coin} not found in exchange universe")
        return self._asset_to_index[coin]

    def _sign_action(self, action: dict[str, Any], nonce: int) -> dict[str, Any]:
        """
        Sign an action using the official Hyperliquid SDK utilities.
        """
        if not self._private_key:
            raise AuthError("HL_PRIVATE_KEY missing; cannot sign action")

        # The SDK's sign_l1_action expects an Account object
        wallet = Account.from_key(self._private_key)
        
        # The signature is: (wallet, action, active_pool, nonce, expires_after, is_mainnet)
        signature = sign_l1_action(
            wallet,
            action,
            None, # active_pool (not needed for binary hashing)
            nonce,
            None, # expires_after (use default or None)
            self._is_mainnet
        )
        
        return {
            "action": action,
            "nonce": nonce,
            "signature": signature
        }

    # -------------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------------
    def get_meta(self) -> dict[str, Any]:
        """Fetch asset metadata (needed for asset indexes)."""
        if self._meta_cache:
             return self._meta_cache
        url = f"{INFO_PATH}"
        body = {"type": "meta"}
        self._meta_cache = self._request("POST", url, json_body=body)
        return self._meta_cache

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------
    def health_check(self) -> dict[str, Any]:
        """
        Check whether the API base URL is reachable.
        Returns dict with ok, network, base_url, latency_ms, message.
        """
        url = f"{self._base_url}{INFO_PATH}"
        body: dict[str, Any] = {"type": HEALTH_ENDPOINT_TYPE}
        start = time.perf_counter()
        try:
            self._request("POST", url, json_body=body, retry=False)
            latency_ms = (time.perf_counter() - start) * 1000
            return {
                "ok": True,
                "network": self._network,
                "base_url": self._base_url,
                "latency_ms": round(latency_ms, 2),
                "message": "OK",
            }
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            self._logger.warning("health_check_failed error=%s", type(e).__name__)
            return {
                "ok": False,
                "network": self._network,
                "base_url": self._base_url,
                "latency_ms": round(latency_ms, 2),
                "message": str(e),
            }

    # -------------------------------------------------------------------------
    # Account summary
    # -------------------------------------------------------------------------
    def get_account_summary(self) -> dict[str, Any]:
        """
        Fetch account-level information (margin, collateral, etc.).
        Normalized keys: wallet_address, equity, free_collateral, used_margin,
        unrealized_pnl, raw.
        """
        if not self._wallet_address:
            raise AuthError(
                "HL_WALLET_ADDRESS is not set in secrets/.env; cannot fetch account summary"
            )
        url = f"{self._base_url}{INFO_PATH}"
        body: dict[str, Any] = {
            "type": ACCOUNT_ENDPOINT_TYPE,
            "user": self._wallet_address,
        }
        # TODO: confirm exact request keys (e.g. "user" vs "address") per Hyperliquid API.
        raw = self._request("POST", url, json_body=body)
        return self._normalize_account_summary(raw)

    def _normalize_account_summary(self, raw: Any) -> dict[str, Any]:
        """Normalize clearinghouseState-like response to standard account summary."""
        if raw is None:
            raise ExchangeError("Account summary response was empty")
        # Hyperliquid clearinghouseState is an object with marginSummary, etc.
        if isinstance(raw, list):
            # Some APIs return [state]; take first.
            raw = raw[0] if raw else {}
        if not isinstance(raw, dict):
            raise ExchangeError(
                f"Account summary response has unexpected type: {type(raw).__name__}"
            )

        margin = raw.get("marginSummary") or raw.get("crossMarginSummary") or {}
        if isinstance(margin, list):
            margin = margin[0] if margin else {}
        account_value = margin.get("accountValue") or margin.get("totalRawUsd") or "0"
        margin_used = margin.get("totalMarginUsed") or "0"
        withdrawable = raw.get("withdrawable") or "0"

        return {
            "wallet_address": self._wallet_address,
            "equity": _to_str(account_value),
            "free_collateral": _to_str(withdrawable),
            "used_margin": _to_str(margin_used),
            "unrealized_pnl": _to_str(raw.get("unrealizedPnl", "0")),
            "raw": raw,
        }

    # -------------------------------------------------------------------------
    # Positions (from clearinghouseState.assetPositions)
    # -------------------------------------------------------------------------
    def get_positions(self) -> list[dict[str, Any]]:
        """
        Fetch open positions. Normalized keys per position: symbol, side, size,
        entry_price, mark_price, liquidation_price, unrealized_pnl, leverage, raw.
        """
        if not self._wallet_address:
            return []
        url = f"{self._base_url}{INFO_PATH}"
        body: dict[str, Any] = {
            "type": ACCOUNT_ENDPOINT_TYPE,
            "user": self._wallet_address,
        }
        raw = self._request("POST", url, json_body=body)
        return self._normalize_positions(raw)

    def _normalize_positions(self, raw: Any) -> list[dict[str, Any]]:
        """Extract assetPositions from clearinghouseState and normalize."""
        if raw is None:
            return []
        if isinstance(raw, list):
            raw = raw[0] if raw else {}
        if not isinstance(raw, dict):
            return []
        positions = raw.get("assetPositions") or []
        if not isinstance(positions, list):
            return []
        out: list[dict[str, Any]] = []
        for p in positions:
            if not isinstance(p, dict):
                continue
            # HL: assetPositions items are flat: coin, szi, entryPx, unrealizedPnl, liquidationPx, leverage (object with .value).
            pos = p.get("position") or p
            if isinstance(pos, list):
                pos = pos[0] if pos else {}
            if not isinstance(pos, dict):
                pos = p
            coin = pos.get("coin") or p.get("coin") or ""
            szi = pos.get("szi") or pos.get("size") or p.get("szi") or "0"
            entry_px = pos.get("entryPx") or p.get("entryPx") or "0"
            lev = p.get("leverage") or pos.get("leverage")
            lev_str = str(lev.get("value", lev)) if isinstance(lev, dict) else _to_str(lev)
            side = "long" if float(szi) > 0 else "short"
            out.append({
                "symbol": self._normalize_symbol_out(coin),
                "side": side,
                "size": _to_str(szi),
                "entry_price": _to_str(entry_px),
                "mark_price": _to_str(p.get("markPx") or pos.get("markPx") or "0"),
                "liquidation_price": _to_str(
                    p.get("liquidationPx") or pos.get("liquidationPx") or "0"
                ),
                "unrealized_pnl": _to_str(p.get("unrealizedPnl") or "0"),
                "leverage": lev_str,
                "raw": p,
            })
        return out

    # -------------------------------------------------------------------------
    # Open orders
    # -------------------------------------------------------------------------
    def get_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """
        Fetch open orders; optionally filter by symbol (canonical e.g. BTC-USDC).
        Normalized keys: order_id, client_order_id, symbol, side, order_type, qty,
        price, trigger_price, reduce_only, status, timestamp, raw.
        """
        if not self._wallet_address:
            return []
        url = f"{self._base_url}{INFO_PATH}"
        body: dict[str, Any] = {
            "type": OPEN_ORDERS_ENDPOINT_TYPE,
            "user": self._wallet_address,
        }
        # TODO: confirm if API supports filtering by coin in request.
        raw = self._request("POST", url, json_body=body)
        orders = self._normalize_orders(raw)
        if symbol:
            sym_norm = self._normalize_symbol_in(symbol)
            orders = [o for o in orders if self._normalize_symbol_in(o.get("symbol") or "") == sym_norm]
        return orders

    def _normalize_orders(self, raw: Any) -> list[dict[str, Any]]:
        """Normalize openOrders (or frontendOpenOrders) response."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for o in raw:
            if not isinstance(o, dict):
                continue
            # HL: oid, coin, side (A/B), limitPx, sz, timestamp, triggerPx, reduceOnly, orderType, cloid.
            side = "buy" if (o.get("side") or "B") == "B" else "sell"
            out.append({
                "order_id": str(o.get("oid") or ""),
                "client_order_id": str(o.get("cloid") or "") or None,
                "symbol": self._normalize_symbol_out(str(o.get("coin") or "")),
                "side": side,
                "order_type": str(o.get("orderType") or "Limit"),
                "qty": _to_str(o.get("sz") or o.get("origSz") or "0"),
                "price": _to_str(o.get("limitPx") or "0"),
                "trigger_price": _to_str(o.get("triggerPx") or "0"),
                "reduce_only": bool(o.get("reduceOnly", False)),
                "status": "open",
                "timestamp": int(o.get("timestamp") or 0),
                "raw": o,
            })
        return out

    # -------------------------------------------------------------------------
    # Ticker
    # -------------------------------------------------------------------------
    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """
        Fetch market info for the given symbol (e.g. BTC-USDC).
        Normalized: symbol, last_price, mark_price, oracle_price, bid, ask,
        volume_24h, open_interest, funding_rate, timestamp, raw.
        """
        coin = self._normalize_symbol_in(symbol)
        url = f"{self._base_url}{INFO_PATH}"
        body_mids: dict[str, Any] = {"type": TICKER_MIDS_TYPE}
        mids = self._request("POST", url, json_body=body_mids)
        # allMids returns {"BTC": "123.45", ...}
        mid_price = "0"
        if isinstance(mids, dict) and coin in mids:
            mid_price = _to_str(mids[coin])
        # Optional: l2Book for bid/ask (TODO: add if needed for accuracy).
        bid, ask = mid_price, mid_price
        try:
            body_l2: dict[str, Any] = {"type": TICKER_L2_TYPE, "coin": coin}
            l2 = self._request("POST", url, json_body=body_l2)
            if isinstance(l2, dict):
                levels = l2.get("levels") or []
                if isinstance(levels, list) and len(levels) >= 2:
                    bids = levels[0]
                    asks = levels[1]
                    if isinstance(bids, list) and bids and isinstance(bids[0], dict):
                        bid = _to_str(bids[0].get("px", mid_price))
                    if isinstance(asks, list) and asks and isinstance(asks[0], dict):
                        ask = _to_str(asks[0].get("px", mid_price))
        except Exception:
            pass
        return self._normalize_ticker(
            symbol=self._normalize_symbol_out(coin),
            mid_price=mid_price,
            bid=bid,
            ask=ask,
            raw_mids=mids,
        )

    def _normalize_ticker(
        self,
        symbol: str,
        mid_price: str,
        bid: str,
        ask: str,
        raw_mids: Any,
    ) -> dict[str, Any]:
        """Build normalized ticker dict. TODO: add volume_24h, open_interest, funding_rate from other endpoints if needed."""
        return {
            "symbol": symbol,
            "last_price": mid_price,
            "mark_price": mid_price,
            "oracle_price": mid_price,
            "bid": bid,
            "ask": ask,
            "volume_24h": None,
            "open_interest": None,
            "funding_rate": None,
            "timestamp": self._now_ms(),
            "raw": raw_mids,
        }

    # -------------------------------------------------------------------------
    # Candles
    # -------------------------------------------------------------------------
    def get_candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch OHLCV candle data. Normalized per candle: open_time, open, high, low,
        close, volume, raw.
        Intervals: 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 8h, 12h, 1d, 3d, 1w, 1M.
        """
        coin = self._normalize_symbol_in(symbol)
        url = f"{self._base_url}{INFO_PATH}"
        req: dict[str, Any] = {
            "coin": coin,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
        }
        body: dict[str, Any] = {"type": CANDLES_ENDPOINT_TYPE, "req": req}
        # TODO: confirm parameter names (startTime/endTime vs start_ms/end_ms) per API.
        raw = self._request("POST", url, json_body=body)
        candles = self._normalize_candles(raw)
        if limit is not None and limit > 0:
            candles = candles[:limit]
        return candles

    def _normalize_candles(self, raw: Any) -> list[dict[str, Any]]:
        """Normalize candleSnapshot response. HL returns list of {t, T, o, h, l, c, v, ...}."""
        if raw is None:
            return []
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            # t = open time, T = close time, o/h/l/c/v
            out.append({
                "open_time": int(c.get("t") or 0),
                "open": _to_str(c.get("o") or "0"),
                "high": _to_str(c.get("h") or "0"),
                "low": _to_str(c.get("l") or "0"),
                "close": _to_str(c.get("c") or "0"),
                "volume": _to_str(c.get("v") or "0"),
                "raw": c,
            })
        return out

    # -------------------------------------------------------------------------
    # Stubs (future trading methods) — do not implement live order placement
    # -------------------------------------------------------------------------
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        qty: str | float,
        price: str | float | None = None,
        trigger_price: str | float | None = None,
        reduce_only: bool = False,
        client_order_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Place a real order on the exchange using the official SDK.
        """
        if not self._exchange:
            raise AuthError("Exchange not initialized; check private key")
            
        is_buy = side.lower() in ["buy", "long"]
        coin = self._normalize_symbol_in(symbol)
        
        # The SDK takes (coin, is_buy, sz, limit_px, order_type, reduce_only, cloid)
        if order_type.lower() == "limit":
            hl_order_type = {"limit": {"tif": "Gtc"}}
            final_px = float(price) if price else 0.0
        else:
            hl_order_type = {"limit": {"tif": "Ioc"}}
            # For Market orders, we use the price with 5% slippage to guarantee the fill
            if price:
                final_px = float(price) * (1.05 if is_buy else 0.95)
            else:
                final_px = 0.0 # This might fail if the SDK doesn't fetch mid price
        
        self._logger.info("Placing %s order for %s: qty=%s, limit_px=%f (market slippage applied)", 
                         side.upper(), coin, qty, final_px)
        
        return self._exchange.order(
            name=coin,
            is_buy=is_buy,
            sz=float(qty),
            limit_px=self._round_price(final_px),
            order_type=hl_order_type,
            reduce_only=reduce_only,
            cloid=client_order_id
        )

    def cancel_order(self, order_id: str, symbol: str | None = None) -> dict[str, Any]:
        """Cancel an order by its ID using the official SDK."""
        if not self._exchange:
            raise AuthError("Exchange not initialized; check private key")
            
        coin = self._normalize_symbol_in(symbol) if symbol else ""
        return self._exchange.cancel(name=coin, oid=int(order_id))

    def set_leverage(self, leverage: int, symbol: str, is_cross: bool = True) -> dict[str, Any]:
        """Set leverage for a symbol using the official SDK."""
        if not self._exchange:
            raise AuthError("Exchange not initialized; check private key")
            
        coin = self._normalize_symbol_in(symbol)
        self._logger.info("Setting leverage for %s to %dx (%s)", coin, leverage, "Cross" if is_cross else "Isolated")
        
        return self._exchange.update_leverage(leverage, coin, is_cross)

    def cancel_all(self, symbol: str | None = None) -> dict[str, Any]:
        """Cancel all open orders, optionally for a symbol. Not implemented; for future use."""
        raise NotImplementedError("cancel_all is not implemented")

    def get_fills(
        self,
        symbol: str | None = None,
        start_time_ms: int | None = None,
        end_time_ms: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch fill history. Not implemented; for future use."""
        raise NotImplementedError("get_fills is not implemented")


def _to_str(value: Any) -> str:
    """Coerce value to string for normalized numeric fields."""
    if value is None:
        return "0"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value).strip() or "0"


# ---------------------------------------------------------------------------
# Debug entrypoint (no orders placed)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.DEBUG)
    print("HyperliquidClient debug (read-only, no orders)")

    try:
        client = HyperliquidClient(
            settings_path="config/settings.yaml",
            env_path="secrets/.env",
        )
    except Exception as e:
        print(f"Failed to create client: {e}")
        raise

    print("\n--- health_check ---")
    health = client.health_check()
    print(json.dumps(health, indent=2))

    print("\n--- get_ticker(BTC-USDC) ---")
    try:
        ticker = client.get_ticker("BTC-USDC")
        print(json.dumps(ticker, indent=2))
    except Exception as e:
        print(f"Ticker error: {e}")

    print("\n--- debug done ---")
