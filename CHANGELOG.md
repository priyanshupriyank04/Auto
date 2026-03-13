# Changelog - Branch: lakshay

All notable changes to the Hyperliquid Breakout Strategy bot.

## [Added]
- **Breakout Arming Safety**: Introduced `breakout_armed` logic. The bot now requires the price to enter the defined morning range before it "arms" itself for a breakout trigger. This prevents immediate accidental trades if the bot starts when the price is already outside the range.
- **Historical Replay on Startup**: Implemented `_replay_today_candles` in the live runner. The bot now fetches and processes all 5-minute candles starting from 08:00 AM IST on startup to automatically reconstruct the morning range.
- **Step-wise Trailing Stop Loss**: Added a dynamic trailing SL. For every 1x range size of profit achieved, the Stop Loss "inches up" (for longs) or "inches down" (for shorts) by one range size to lock in profits.
- **TradeLogger (JSON Reporting)**: Created a new `TradeLogger` class and `logs/trades/` directory. The bot now generates human-readable, daily JSON files (`YYYY-MM-DD_trades.json`) that audit range identification, trade entries, and trade exits.
- **Session Auto-Close**: Added logic to `process_live_price` to emit a `market_close` signal at 15:30 IST if a trade is active, ensuring no positions are carried overnight.
- **First Trade Auto-Arm**: Updated range identification to automatically arm the strategy for the first trade of the session. This ensures that the very first breakout is captured even if the price is already outside the range when the morning box is defined.
- **High-Frequency Persistence**: Enhanced the state persistence logic to save the `virtual_sl` immediately when it trails and the `breakout_armed` status when it changes. This ensures 100% crash recovery for trailing stops and arming.
- **Auto-Shutdown**: The live runner and paper runner now detect when the session has ended and all positions are closed, allowing them to shut down cleanly without manual intervention.
- **Paper Trading Synchronization**: Updated `run_breakout_paper.py` to match the live runner's logic, including historical replay, trailing stop-losses, and breakout arming.
- **Human-Readable Test Logs**: Added `TradeLogger` to the paper runner, directing test logs to `logs/test/` to keep them separate from live trading logs.
- **Range Calculation Unit Test**: Created `tests/test_range_calculation.py` to programmatically verify that the strategy correctly identifies the morning range based on candle colors and price extremes.
- **Order Lifecycle Test**: Created `tests/test_order_lifecycle.py` to verify API connectivity and authentication by placing a distant, safe limit order and then immediately cancelling it.
- **Morning Range Debugger**: Created `tests/debug_morning_range.py` to inspect and verify the candles used for range calculation directly from the SQLite database.
- **Session Reset Tool**: Created `tests/reset_session.py` to clear the persisted strategy state and trade history for the current day, allowing for a fresh start or logic switching (e.g., exiting Test Mode).
- **Heartbeat Log File**: Implemented `logs/heartbeat.txt` in the live runner. This file is updated every 5 seconds with a single-line summary of the current price, range, and trade status for quick human inspection.
- **1m Data Fetcher**: Created `tests/fetch_1m_data.py` to fetch historical 1-minute OHLCV candles for a specific date and save them to CSV.
- **Daily Backtester**: Created `tests/backtest_day.py` to simulate the live strategy on historical 1m CSV data, generating PnL and trade audit logs within the `tests/` directory.
- **Custom Trade Date**: Added `DATE_TO_TRADE` configuration to the live runner. This allows forcing the bot to trade a specific day while isolating the state in the database (`breakout_strategy_YYYY-MM-DD`).
- **State Recovery**: Added `load_state` to the strategy engine to restore `sl_count`, `tp_hit`, and `breakout_armed` status from the SQLite database upon restart.

## [Changed]
- **Market Order Execution**: Switched all entry and exit orders from `limit` to `market`. This ensures guaranteed fills during fast-moving breakouts and protects against slippage on Stop Loss hits.
- **Session End Handling**: Updated the 15:30 IST check to proactively trigger a Market Sell/Buy if in a trade, rather than simply stopping the strategy logic.
- **Re-entry Prevention**: Modified trade entry logic to reset the `breakout_armed` flag immediately. If a trade is stopped out, the price must return *inside* the range to arm the next trade.
- **Daily PnL Logs**: Updated `PnLLogger` to generate date-based CSV files (e.g., `logs/pnl_YYYY-MM-DD.csv`) instead of a single `pnl.csv`. This prevents log growth issues and organizes trade data by day.
- **Session Times**: Standardized session end time to **15:30 IST** across the strategy engine and documentation.
- **Documentation Sync**: Fully updated `LIVE_STRATEGY.md` and `CHANGELOG.md` to align with the current codebase, including the updated arming logic, constants, and file structure.

## [Fixed]
- **Late-Start Range Discovery**: Fixed the issue where starting the bot in the afternoon would cause it to miss the morning 08:00 AM IST range.
- **Orphaned Positions**: Resolved the risk of trades staying open past the daily session end by implementing the automatic square-off logic.
- **Restart Log Overwriting**: Fixed the trade logging behavior to append to existing daily JSON files instead of overwriting them during a restart.
- **SQLite :memory: Handling**: Fixed a bug in `StateStore` that caused an actual file named `:memory:` to be created on disk during unit tests. It now correctly uses volatile RAM for in-memory databases.
- **BTC Tick Size Compliance**: Implemented automatic price rounding in the strategy engine. All price-related calculations (Range, Entry, SL, TP) are now rounded to the nearest integer (tick size 1.0) to prevent exchange rejections on live orders.
- **Fixed Trade Size**: Updated `PositionManager` to use a fixed trade size of $11.0. Removed the local equity cap to ensure the bot attempts the trade even if the API reports zero balance (letting the exchange handle the final validation).
- **Manual Range Override (Test Mode)**: Added `TEST_MODE` and `TEST_RANGE` variables to `run_breakout_live.py`. This allows developers to bypass the morning range identification and manually set a trading range for logic testing.
- **Mandatory 1x Leverage**: The bot now automatically forces the account leverage to 1x Cross on startup for the trading symbol, ensuring maximum safety for small accounts.
- **Bypassed Equity Safety Check**: Removed the local block that prevented order placement when reported equity was under $10. The bot will now attempt every trade signal regardless of local balance reporting.
