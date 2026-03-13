# AGENTS.md – Trading Bot Project Memory & Architecture

**Project Name**: Pan's Algorithmic Trading Bot (🐾)  
**Goal**: Build a low-frequency, trend-following stock trading system in Python  
**Style**: Conservative – infrequent trades, strong trend filters, paper trading first  
**Current Date in Conversation**: March 2026  
**Brokerage**: Alpaca (paper mode first)  
**Data Provider**: Finnhub (primary), alternatives: Polygon, Alpha Vantage  
**Database**: DuckDB (file-based, columnar, lightweight)  
**Indicators Library**: TA-Lib  
**Scheduling**: `schedule` library (every 10–15 min during market hours)  
**Development Environment**: Cursor + Python 3.11/3.12 + venv

## Core Philosophy & Constraints

- **Not a day trader** → Avoid PDT rule (no 4+ day trades in 5 days under $25k)
- Trade only on **strong, confirmed trends** (ADX > 25)
- Multiple layered confirmations before entry
- Small position sizes during development/testing
- Paper trading mandatory until thoroughly backtested
- No high-frequency → checks ~every 10 min, logic looks at hourly/daily context

## Project Folder Structure (as of latest agreement)
trading-bot/
├── main.py                 # Scheduler + main loop
├── config.py               # SYMBOLS list, constants, intervals, RISK_PCT_PER_TRADE
├── data_fetch.py           # fetch_and_store(symbol) → Finnhub → DuckDB
├── backfill.py             # Historical backfill into trends_backtest for backtesting
├── backtest.py             # Backtest engine (reuses analyze_trends + trading rules)
├── analysis.py             # analyze_trends(symbol) ← ALL indicators, returns atr_14 for sizing
├── trading.py              # execute_trade(symbol, analysis) ← Alpaca, position sizing
├── utils.py                # is_market_open(), logger, helpers
├── .env                    # API keys (gitignore!)
├── requirements.txt
├── trends.db               # DuckDB file (gitignore or .duckdb/)
├── logs/                   # bot.log + rotation if needed
└── AGENTS.md               # ← this file


## Key Indicators & Their Roles (current version)

| Indicator              | Library   | Period/Params          | Role in Strategy                                      | Threshold / Signal Used                     |
|-----------------------|-----------|------------------------|-------------------------------------------------------|---------------------------------------------|
| ADX                   | TA-Lib    | 14                     | Primary trend strength filter                         | > 25 = strong trend (gate for all trades)   |
| +DI / -DI             | TA-Lib    | 14                     | Direction & crossover timing                          | +DI > -DI = up, crossover detection         |
| Parabolic SAR         | TA-Lib    | acc=0.02, max=0.20     | Trend direction + dynamic trailing stop / flip signal | Price > SAR = bullish, flip = reversal      |
| Bollinger Bands       | TA-Lib    | 20, 2 std devs         | Volatility context + band riding / squeeze avoidance  | Price near upper = strong up, squeeze <4%   |
| RSI                   | TA-Lib    | 14                     | Momentum / overbought-oversold filter                 | >55 up, <35 down                            |
| MACD                  | TA-Lib    | 12,26,9                | Momentum confirmation                                 | MACD > signal = bullish                     |
| SMA                   | TA-Lib    | 50                     | Longer-term trend baseline                            | Price > SMA_50 = bullish bias               |
| Yesterday comparison  | Pandas    | ~1440 min (24h)        | Avoid entries when price ≈ yesterday                  | Change < 2% = "similar"                     |

## Current Entry / Exit Logic Summary

**Buy (Long) Condition** (all must be true)

- Strong trend: `ADX > 25`
- Uptrend direction: `+DI > -DI`
- Price above Parabolic SAR (`sar_below_price`)
- Price above BB middle band + near/touching upper band
- Fresh bullish signal: `bullish_crossover` **or** `sar_flipped_to_bull`
- Momentum: `MACD > signal`, `RSI > 55`, `close > SMA_50`
- Not similar to yesterday (`abs(change) ≥ 2%`)
- No BB squeeze (avoid low-vol entries)

**Sell / Exit Condition** (any true → consider exit)

- `sar_above_price` or `sar_flipped_to_bear`
- `near_lower_band` or strong bearish move
- `dive_bombing`: downtrend + RSI < 35 + sharp drop
- `bearish_crossover`
- (Future: trailing stop via SAR or % loss)

## Important Files – Where the Logic Lives

- **`analysis.py`**  
  → Contains the massive `analyze_trends(symbol)` function  
  → All TA-Lib calls, DataFrame manipulations, boolean flags  
  → Returns rich dict with every signal/metric

- **`trading.py`**  
  → `execute_trade(symbol, analysis)`  
  → Alpaca TradingClient initialization  
  → Buy/sell order submission logic (Market orders, qty=1 for now)

- **`data_fetch.py`**  
  → `fetch_and_store(symbol)`  
  → Finnhub candle API call (1-min resolution recommended)  
  → Upsert into DuckDB table `trends`

- **`main.py`**  
  → Simple schedule loop calling `job()` every X minutes  
  → `job()` loops over SYMBOLS → fetch → analyze → trade

## Next Likely Improvements (conversation backlog)

- Backtesting harness (historical data replay)
- Position sizing (risk % per trade)
- Max daily/weekly trade limits
- Notifications (email / Discord on trade)
- BB squeeze + breakout detection
- ADX slope / rising trend filter
- Multi-timeframe confirmation
- Error recovery & rate-limit handling
- Docker + VPS deployment instructions
- Logging rotation & alerts on exceptions

## Nice-to-haves (next steps toward a more robust bot)

- **Backtesting** — Implemented: `backfill.py` + `backtest.py`; replay historical data with current rules; measure P&L, drawdown, win rate. Run after backfilling into `trends_backtest`.
- **Position sizing** — Implemented: `config.RISK_PCT_PER_TRADE` (e.g. 1% of equity); ATR/stop-based qty in `trading._compute_buy_qty`; set to `None` for fixed qty=1.
- **Simple monitoring** — Implemented: `report.py` prints account equity, positions, unrealized P&L, daily/weekly trade counts, and recent trade log. Run on demand or on a schedule.
- **Alerts** — Implemented: `alerts.py` sends Discord webhook messages on each trade (BUY/SELL/stop-loss) and on errors; optional email for errors only. Set `DISCORD_WEBHOOK_URL` (and optionally `ALERT_EMAIL_*`) in `.env`.

## Quick Commands Reminder

```bash
# Activate venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows

# Run bot
python main.py

# Install missing deps
pip install -r requirements.txt