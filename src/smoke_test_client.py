r"""
Smoke test for Hyperliquid read-only client. NO ORDER PLACEMENT.

Windows — run from project root (crypto-auto):

  python -m venv .venv
  .\.venv\Scripts\activate
  pip install -r requirements.txt
  copy secrets\.env.example secrets\.env
  (edit secrets\.env and set HL_WALLET_ADDRESS; HL_PRIVATE_KEY optional for read-only)
  python src\smoke_test_client.py

Exits cleanly; only runs health_check, get_ticker, get_candles, get_account_summary.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure project root is on path when running as script
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from src.hyperliquid_client import AuthError, HyperliquidClient


def _main() -> None:
    print("Loading Hyperliquid client (read-only)...")
    client = HyperliquidClient(
        settings_path="config/settings.yaml",
        env_path="secrets/.env",
    )

    # 1) Health check
    print("\n--- Health check ---")
    health = client.health_check()
    print(json.dumps(health, indent=2))
    if not health.get("ok"):
        print("Health check failed. Exiting.")
        sys.exit(1)

    # 2) Ticker for BTC-USDC
    print("\n--- Ticker BTC-USDC ---")
    try:
        ticker = client.get_ticker("BTC-USDC")
        print(json.dumps(ticker, indent=2))
    except Exception as e:
        print(f"Ticker error: {e}")

    # 3) Candles: last 2 hours, 5m interval
    print("\n--- Candles BTC-USDC (last 2h, 5m) ---")
    try:
        end_ms = client._now_ms()
        start_ms = end_ms - (2 * 60 * 60 * 1000)
        candles = client.get_candles("BTC-USDC", "5m", start_ms, end_ms, limit=10)
        print(f"Received {len(candles)} candles")
        for c in candles[:3]:
            print(json.dumps(c, indent=2))
        if len(candles) > 3:
            print("...")
    except Exception as e:
        print(f"Candles error: {e}")

    # 4) Account summary (requires HL_WALLET_ADDRESS; auth may fail if not set)
    print("\n--- Account summary ---")
    try:
        summary = client.get_account_summary()
        print(json.dumps(summary, indent=2))
    except AuthError as e:
        print(f"Auth/setup: {e}")
        print("Set HL_WALLET_ADDRESS in secrets/.env to test account summary.")
    except Exception as e:
        print(f"Account summary error: {e}")

    print("\n--- Smoke test done (no orders placed). ---")


if __name__ == "__main__":
    _main()
