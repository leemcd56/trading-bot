# Pan's Algorithmic Trading Bot

A low-frequency, trend-following stock trading system in Python. Uses **Alpaca** for execution (paper trading first), **Finnhub** for 1-minute candles, **DuckDB** for storage, and **TA-Lib** for technical indicators. The bot runs on a schedule during US market hours and only trades when multiple confirmations align.

**Philosophy:** Conservative—infrequent trades, strong trend filters, no “buy the dip.” Paper trade until thoroughly validated.

---

## Setup

### Requirements

- **Python 3.11+** (3.12 recommended)
- **TA-Lib** must be installed at the system level before pip (it has C extensions):
  - **macOS:** `brew install ta-lib` then `pip install TA-Lib`
  - **Linux:** install `ta-lib` dev package for your distro, then `pip install TA-Lib`
  - **Windows:** use a prebuilt wheel or build from source; see [TA-Lib Python](https://github.com/mrjbq7/ta-lib)

### 1. Clone and create a virtual environment

```bash
cd trading-bot
python3 -m venv venv
source venv/bin/activate   # macOS/Linux
# or: venv\Scripts\activate   # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Environment variables

Copy the example env file and fill in your keys:

```bash
cp .env.example .env
```

Edit `.env`:

- **Alpaca** (paper): `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`. Use [Alpaca paper trading](https://app.alpaca.markets/paper/dashboard) and keep `ALPACA_BASE_URL=https://paper-api.alpaca.markets` for paper.
- **Finnhub:** `FINNHUB_API_KEY` from [Finnhub](https://finnhub.io/).

Never commit `.env`; it is gitignored.

### 4. Run the bot

```bash
python main.py
```

The bot runs every 10 minutes (configurable) while the US market is open (9:30 AM–4:00 PM ET, Mon–Fri). Logs go to `logs/bot.log` and the console.

---

## Configuration

Edit **`config.py`** to change:

| Variable | Default | Description |
|----------|---------|--------------|
| `SYMBOLS` | `['AAPL', 'TSLA', 'GOOG', 'MSFT']` | Watchlist / symbols to trade |
| `CHECK_INTERVAL_MINUTES` | `10` | Minutes between each run |
| `DB_PATH` | `'trends.db'` | DuckDB file for candles and trade log |
| `MAX_DAILY_TRADES` | `3` | Max new orders in rolling 24 hours |
| `MAX_WEEKLY_TRADES` | `8` | Max new orders in rolling 7 days |
| `MAX_OPEN_POSITIONS` | `4` | Max symbols held at once (no new BUY above this) |
| `STOP_LOSS_PCT` | `0.05` | Sell if position is down 5% from average entry (e.g. `0.03` = 3%) |

---

## What’s Implemented

### Data pipeline

- **`data_fetch.fetch_and_store(symbol)`**  
  Fetches 1-minute candles from Finnhub for the last ~2000 minutes, upserts into DuckDB table `trends` (symbol, timestamp, open, high, low, close, volume). Retries on 429 (rate limit) and 5xx with exponential backoff.

### Analysis (indicators and signals)

- **`analysis.analyze_trends(symbol, connection=None)`**  
  Loads up to 300 bars from `trends`, computes indicators, and returns a dict of boolean signals and `current_price`. Optional `connection` is for tests with spoofed data.

**Indicators (TA-Lib):**

- **ADX (14)** — trend strength; trade only when ADX > 25.
- **+DI / -DI (14)** — direction and crossovers (bullish when +DI > -DI).
- **Parabolic SAR** (acc=0.02, max=0.20) — trend direction and flip signals.
- **Bollinger Bands (20, 2)** — near upper/lower band, squeeze (< 4% width).
- **RSI (14)** — momentum filter (e.g. > 55 for buy, < 35 for dive-bomb).
- **MACD (12, 26, 9)** — momentum confirmation (MACD > signal = bullish).
- **SMA (50)** — longer-term trend (price > SMA_50 for buy bias).
- **Yesterday comparison** — avoid new entries when price is within 2% of prior day’s close.

**Signals returned (examples):** `strong_trend`, `uptrend`, `sar_below_price`, `sar_above_price`, `near_upper_band`, `near_lower_band`, `bb_squeeze`, `bullish_crossover`, `bearish_crossover`, `sar_flipped_to_bull`, `sar_flipped_to_bear`, `trending_up_a_lot`, `similar_to_yesterday`, `dive_bombing`, `current_price`.

**Data quality:** If the latest bar is older than 45 minutes (production only), analysis returns `None` so the bot doesn’t trade on stale data.

### Trading logic

- **`trading.execute_trade(symbol, analysis)`**  
  Uses the analysis dict to decide whether to submit orders. Order of checks:

1. **Risk limits** — If daily or weekly trade cap is reached, or open positions are at max, no new BUY; log and return.
2. **Stop-loss** — If the symbol has an open position and `current_price <= entry * (1 - STOP_LOSS_PCT)`, submit a market SELL for the full position, log, and return.
3. **BUY** — Only if all of: `strong_trend`, `trending_up_a_lot`, `near_upper_band`, `sar_below_price`, (bullish crossover or SAR flip to bull), not `similar_to_yesterday`, not `bb_squeeze`, and under `MAX_OPEN_POSITIONS`. Submits market BUY, qty=1.
4. **SELL** — If any of: `near_lower_band`, `sar_above_price`, `sar_flipped_to_bear`, `dive_bombing`, `bearish_crossover`, and we have a position, submit market SELL for full qty.
5. Otherwise log “No signal” with the reasons (e.g. which condition failed).

**Stop-loss:** Uses Alpaca’s `avg_entry_price` and `analysis['current_price']`. When price is down by at least `STOP_LOSS_PCT` from entry, the position is closed with a market sell. Checked every run before other signals.

**Trade log:** Every submitted order (BUY or SELL) is appended to DuckDB table `trade_log` (timestamp_utc, symbol, side). Daily and weekly limits are computed from this table so they persist across restarts.

### Scheduler and utils

- **`main.py`** — Runs `job()` every `CHECK_INTERVAL_MINUTES`. `job()` skips when the market is closed; for each symbol it calls fetch → analyze → execute_trade and logs errors per symbol.
- **`utils`** — `is_market_open()` (US/Eastern, Mon–Fri 9:30–16:00), logger (file + console), and `logs/` directory created on startup.

---

## Project structure

```
trading-bot/
├── main.py           # Scheduler and main loop
├── config.py         # Symbols, intervals, risk and stop-loss settings
├── data_fetch.py     # Finnhub → DuckDB (fetch_and_store)
├── analysis.py       # Indicators and signals (analyze_trends)
├── trading.py        # Order logic, limits, stop-loss (execute_trade)
├── utils.py          # is_market_open(), logger
├── requirements.txt
├── .env.example      # Template for API keys
├── .env              # Your keys (gitignored)
├── trends.db         # DuckDB: trends (candles), trade_log (counts)
├── logs/
│   └── bot.log
├── tests/
│   ├── helpers.py    # Spoofed OHLC and test DB helpers
│   ├── test_analysis.py
│   └── test_trading.py
├── AGENTS.md         # Project memory and architecture (for AI/agents)
└── README.md         # This file
```

---

## Tests

Tests use spoofed data and mocks so no live API keys or real orders are needed.

```bash
pip install -r requirements.txt   # includes pytest
python -m pytest tests/ -v
```

- **`test_analysis.py`** — Downtrend produces no buy signal; insufficient data returns `None`; return dict has all keys trading expects.
- **`test_trading.py`** — No order when analysis is missing or no strong trend; no BUY in downtrend; BUY when all conditions met; SELL when conditions met and position exists; no SELL when no position; stop-loss sells when price below threshold; no sell when above stop-loss threshold.

---

## Strategy summary

**Entry (all required):** Strong trend (ADX > 25), uptrend (+DI > -DI), price above SAR and near upper Bollinger Band, fresh bullish signal (crossover or SAR flip), MACD > signal, RSI > 55, price > SMA(50), not similar to yesterday, no BB squeeze, and under position/trade limits.

**Exit (any):** Price below SAR or SAR flip to bear, near lower band, dive-bombing (downtrend + RSI < 35 + sharp drop), bearish +DI/-DI crossover, or **stop-loss** (price down ≥ `STOP_LOSS_PCT` from average entry).

---

## Disclaimer

This bot is for education and paper trading. Use at your own risk. Past behavior and tests do not guarantee future results. Paper trading uses simulated money; switching to live trading can have real financial impact. Ensure you understand the strategy, risk limits, and broker terms before using real capital.
