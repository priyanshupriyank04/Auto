# crypto-auto

Windows-based local setup for a Hyperliquid trading bot. **Read-only** REST/WebSocket for market data and candles; **paper-mode** breakout strategy (signals only, no live orders).

## What’s in the repo

- **`src/hyperliquid_client.py`** — Client that talks to Hyperliquid mainnet: config from `config/settings.yaml`, secrets from `secrets/.env`. Read-only methods (health, ticker, candles, account, positions, open orders) plus stubs for future trading.
- **`src/ws_marketdata.py`** — WebSocket client for live market data (BTC-USDC). Subscribes to l2Book, trades, bbo, activeAssetCtx, allMids; maintains a normalized snapshot in memory; auto-reconnect with backoff.
- **`src/candle_builder.py`** — In-memory 1m and 5m OHLC candle builder. Accepts live tick/price updates, maintains current and recent closed candles (deques), optional bootstrap from REST candles. Thread-safe; no strategy or order logic. Use with `ws_marketdata` for live candles or plug into strategy later.
- **`src/smoke_test_client.py`** — Script that runs a short read-only test (health → ticker → candles → account summary).
- **`src/main_readonly.py`** — Read-only integration runner: starts WebSocket market data, polls latest snapshot, extracts ticks, feeds `CandleBuilder`, and persists state via `StateStore` (SQLite). Persists latest market snapshot, closed 1m/5m candles, runner stats, and component status to `data/bot_state.db`. Logs heartbeats and closed candles. No trading, orders, or risk logic. Use for live market data + candle integration testing.
- **`src/state_store.py`** — SQLite state persistence for the bot: component status, latest market snapshot, closed candles, runner stats. Also owns the paper trading audit tables (`paper_trade_events`, `paper_trades`, `paper_daily_summary`). Creates `data/bot_state.db` and tables automatically. No strategy, websocket, or order logic. Use for saving/loading bot state.
- **`src/db_inspector.py`** — Read-only database inspector for debugging persisted state. Prints DB info, component statuses, latest market snapshot, runner stats, recent 1m/5m candles, and validates candle sequence (duplicates, gaps, order). No writes; use during debugging.
- **`src/bootstrap_history.py`** — Historical candle bootstrap via REST. Fetches recent 1m/5m candles from Hyperliquid, validates and sanitizes them, persists to SQLite, and can bootstrap a `CandleBuilder`. Use to backfill history before or alongside the live runner; no websocket or trading logic.
- **`src/breakout_strategy.py`** — Paper-only breakout strategy engine (IST session 08:00–15:30, weekdays). Scans 5m candles for first opposite-color pair, defines range, emits long/short signals on breakout; tracks virtual TP/SL and daily halt (1 TP or 3 SL). Persists state to SQLite via `StateStore`; no live orders or exchange execution.
- **`src/run_breakout_paper.py`** — Paper trading integration runner. Wires WebSocket market data, `CandleBuilder`, `BreakoutStrategyEngine`, `StateStore`, and `PaperTradeLogger`: replays today’s 5m candles from DB into the strategy, bootstraps the candle builder, then feeds live prices and closed 5m candles continuously. Logs PAIR FOUND, ENTRY LONG/SHORT, STOP LOSS, TAKE PROFIT, TRADING HALTED; persists strategy state, structured paper events, trade history, and runner stats. Run until Ctrl+C for graceful shutdown.
- **`src/paper_trade_logger.py`** — Structured paper trade logger. Writes every meaningful strategy event to `paper_trade_events`, maintains one row per paper trade in `paper_trades` (OPEN/CLOSED_TP/CLOSED_SL/etc.), and keeps per-day IST summaries in `paper_daily_summary` (wins, losses, PnL, halt status). Fully restart-safe via SQLite uniqueness constraints and idempotent updates.

## Setup (once)

From the project root (`crypto-auto`):

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Copy `secrets\.env.example` to `secrets\.env`, then set at least:

- `HL_WALLET_ADDRESS` — your wallet address (needed for account summary; optional for health/ticker/candles).

## What to run

| Command | What it does | What you get |
|--------|----------------|--------------|
| `python src\hyperliquid_client.py` | Quick debug: create client, run health check, then fetch BTC-USDC ticker. | Printed JSON: `ok`, `network`, `base_url`, `latency_ms`, then ticker (symbol, last_price, bid, ask, etc.). No orders. |
| `python src\ws_marketdata.py` | Live WebSocket market data for BTC-USDC. | Connects to Hyperliquid WS, runs ~30s, prints status and snapshot every few seconds, then stops. No orders. |
| `python src\candle_builder.py` | Candle builder demo (no WS). | Simulates ticks across 1m and 5m boundaries; prints current/latest closed candles and stats. Use to verify candle logic. |
| `python src\smoke_test_client.py` | Full read-only smoke test. | 1) Health check. 2) Ticker for BTC-USDC. 3) Last 2h of 5m candles (sample). 4) Account summary (if wallet set). All as printed JSON; no orders. |
| `python -m src.main_readonly` | Read-only integration (live WS + candles + DB). | Starts WebSocket for BTC-USDC, polls snapshot every 0.25s, feeds ticks to CandleBuilder, persists snapshots/candles/runner stats/component status to `data/bot_state.db`, logs heartbeats every 5s. Runs until Ctrl+C; clean shutdown with final DB write and close. |
| `python -m src.state_store` | State store demo. | Uses a temporary DB, inserts sample component status, market snapshot, closed candle, runner stats; reads them back and prints DB info. Cleans up demo DB on exit. |
| `python -m src.db_inspector` | Database inspector (read-only). | Prints DB path and row counts, component statuses, latest market snapshot, runner stats, recent 1m/5m candles, and candle validation (duplicates, gaps, order). Use for debugging persisted state. |
| `python -m src.bootstrap_history` | Historical candle bootstrap. | Fetches last 120 min of 1m and 720 min of 5m candles from Hyperliquid REST, validates them, persists to `data/bot_state.db`, and runs a demo bootstrap of `CandleBuilder`. Prints a summary; run before or alongside the live runner to backfill history. |
| `python -m src.breakout_strategy` | Breakout strategy (paper) demo. | Runs simulated IST day: 5m candles → first opposite-color pair → range → breakout long → TP hit; then 3× SL halt scenario. Persists strategy state to DB; no live orders. |
| `python -m src.run_breakout_paper` | **Paper breakout runner (live).** | Starts WebSocket, replays today’s 5m candles from DB into the strategy, then feeds live prices and closed 5m candles. Logs strategy events (PAIR FOUND, ENTRY, TP/SL, HALTED), persists state and runner stats. Runs until Ctrl+C; graceful shutdown. |
| _(inspect paper logs)_ | **Inspect audit tables (optional).** | Use `python -m src.db_inspector` or a SQLite browser to look at `paper_trade_events` (event history), `paper_trades` (trade lifecycle with PnL), and `paper_daily_summary` (per-day IST summary) in `data/bot_state.db`. |

## Current workflow (brief)

1. Config is read from `config/settings.yaml` (mainnet, timeout, retries, default symbol BTC-USDC).
2. Secrets are read from `secrets/.env` (e.g. `HL_WALLET_ADDRESS`).
3. The client calls Hyperliquid’s public info API (e.g. health, ticker, candles, account/positions/orders).
4. Responses are normalized to simple dicts and returned (with a `raw` field for debugging).
5. No trading or order placement runs; only read-only endpoints are used.
6. When you run `python -m src.main_readonly`, state is persisted to `data/bot_state.db`: latest snapshot, closed 1m/5m candles, runner stats, and component status for `main_readonly` and `ws_marketdata`. Shutdown (Ctrl+C or stop) writes final stats and closes the DB from the main thread.
7. Optionally run `python -m src.bootstrap_history` to backfill historical 1m/5m candles from REST into the same DB; then run `python -m src.db_inspector` to inspect the combined state.
8. **Breakout strategy (paper):** `BreakoutStrategyEngine` in `src/breakout_strategy.py` consumes closed 5m candles and live price; uses IST (Asia/Kolkata), weekdays 08:00–15:30, first opposite-color pair for range; emits paper long/short on breakout; halts after 1 TP or 3 SL. State persisted as `breakout_strategy` in `component_status`.
9. **Paper breakout runner:** Run `python -m src.run_breakout_paper` to run the strategy live in paper mode: it loads today’s closed 5m candles from `data/bot_state.db`, replays them into the strategy, bootstraps the candle builder, then connects to the WebSocket and continuously feeds live prices and newly closed 5m candles. Events (pair found, entry, TP/SL, halted) are logged to both console and `paper_trade_events`, trades are opened/closed in `paper_trades` with PnL, and per-day IST stats are tracked in `paper_daily_summary`. Runner stats (`run_breakout_paper`) are written periodically. For best results, run `python -m src.bootstrap_history` first to backfill today’s candles.

## Requirements

- Python 3.11+
- `requests`, `python-dotenv`, `PyYAML`, `websocket-client`, `tzdata` (see `requirements.txt`)
