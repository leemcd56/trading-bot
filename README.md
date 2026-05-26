# Pan's Algorithmic Trading Bot

A low-frequency, trend-following stock trading system in Python. Uses **Alpaca** for execution (paper trading first), **Yahoo Finance** for daily candles (Finnhub as fallback), **MotherDuck/DuckDB** for storage, **TA-Lib** for technical indicators, and optionally **Financial Modeling Prep** for analyst signal feeds. The bot runs on a schedule during US market hours and only trades when multiple confirmations align.

**Philosophy:** Strong trend filters, no “buy the dip,” and configurable risk appetite via **trading modes** — from nest-egg conservative to bleeding-edge aggressive. Paper trade until thoroughly validated.

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
- **Finnhub (optional fallback):** `FINNHUB_API_KEY` from [Finnhub](https://finnhub.io/). Used if Yahoo Finance fails.
- **MotherDuck (required):** Set `MOTHERDUCK_TOKEN` — the bot uses [MotherDuck](https://motherduck.com) hosted DuckDB exclusively and will not start without it. Data (candles, trade log) lives in the cloud and survives redeploys or machine loss.
- **FMP (optional):** Set `FMP_API_KEY` from [Financial Modeling Prep](https://financialmodelingprep.com/developer/docs) to enable the analyst signal feed. When set, the bot queries today's analyst upgrades/downgrades each hour and trades on them without requiring TA confirmation. Free tier (250 calls/day) is sufficient.
- **Trading mode (optional):** Set `TRADING_MODE` to one of `conservative`, `moderate` (default), `aggressive`, `swing`, or `dormant`. See [Trading Modes](#trading-modes) below.

Never commit `.env`; it is gitignored.

### 4. Run the bot

```bash
python main.py
```

The bot runs every 60 minutes (configurable) while the US market is open (9:30 AM–4:00 PM ET, Mon–Fri). Logs go to `logs/bot.log` and the console.

---

## Trading Modes

Set `TRADING_MODE` in `.env` (or as an env var) to choose a risk profile. All risk parameters come from the selected mode's file in `modes/`. `MAX_DAILY_TRADES` and `MAX_WEEKLY_TRADES` can still be individually overridden via env vars.

| Mode | Daily cap | Weekly cap | Positions | Stop-loss | Notional/trade | Description |
|------|-----------|------------|-----------|-----------|----------------|-------------|
| `conservative` | 1 | 3 | 2 | 7% | $50 | Low frequency, high conviction. Nest-egg building. |
| `moderate` *(default)* | 3 | 8 | 4 | 5% | $75 | Balanced risk/reward. Reasonable activity. |
| `aggressive` | 6 | 15 | 6 | 3% | $150 | High-frequency day trading. Maximize activity and size. |
| `swing` | 2 | 5 | 3 | 8% | $100 | Ride multi-day trends with wide stops; let winners run. |
| `dormant` | 0 | 0 | — | — | — | Analysis and alerts only. No orders are submitted. |

Each mode also controls trailing-stop activation, ADX threshold, and other entry filters. See `modes/<name>.py` for the full parameter list.

**Safety defaults for missing keys**: `config.py` loads all critical risk parameters (ADX threshold, `STOP_LOSS_PCT`, `TRAIL_ACTIVATION_PCT`/`TRAIL_PCT`, `NOTIONAL_PER_TRADE`, etc.) defensively. If a mode file is ever missing a required key (e.g. from manual editing or a new custom mode), the bot falls back to conservative-leaning safe defaults:

- `ADX_STRONG_TREND_THRESHOLD`: 22 (only strong trends)
- `STOP_LOSS_PCT`: 7%, `TRAIL_ACTIVATION_PCT`: 10%, `TRAIL_PCT`: 6% (positions get room to breathe)
- Trade caps and risk % are low
- `NOTIONAL_PER_TRADE`: `None` (enables ATR-based risk sizing in `trading.py` instead of fixed tiny dollar amounts that amplify costs)

A warning is logged at startup naming the exact missing key and file to fix. Aggressive mode gets extra validation (it refuses to start with certain catastrophic combinations and warns loudly on others). This guarantees that ADX, trail, and notional variables (and all other risk params) always have common-sense values — even if someone forgets to set them in a mode file — so the bot cannot silently disable stops or overtrade weak trends.

All built-in modes (`conservative`, `moderate`, `aggressive`, `swing`, `dormant`) are complete; the fallbacks are a last-resort safety net, not a substitute for proper mode definitions. Tests in `tests/test_modes.py` enforce completeness and cover the fallback path.

---

## Configuration

Edit **`config.py`** or set env vars to override individual settings. Risk parameters marked *(from mode)* default to whatever the selected `TRADING_MODE` specifies.

| Variable | Default | Description |
|----------|---------|--------------|
| `SYMBOLS` | `['AAPL', 'TSLA', 'GOOG', 'MSFT']` | Watchlist / symbols to trade (overridable via `WATCH_SYMBOLS` env var) |
| `CHECK_INTERVAL_MINUTES` | `60` | Minutes between each run |
| `DB_PATH` | MotherDuck | Requires `MOTHERDUCK_TOKEN` in `.env`; there is no local file fallback |
| `TRENDS_RETAIN_DAYS` | `365` | Keep this many days of candle data; older rows are pruned each run |
| `TRADE_LOG_RETAIN_DAYS` | `30` | Keep this many days of trade log; older rows pruned (weekly count needs 7+) |
| `MAX_DAILY_TRADES` | *(from mode)* | Max new orders in rolling 24 hours (overridable via env var) |
| `MAX_WEEKLY_TRADES` | *(from mode)* | Max new orders in rolling 7 days (overridable via env var) |
| `MAX_OPEN_POSITIONS` | *(from mode)* | Max symbols held at once (no new BUY above this) |
| `STOP_LOSS_PCT` | *(from mode)* | Sell if position is down this % from average entry |
| `MAX_DAY_TRADES_IN_5_DAYS` | `3` | PDT: max day trades in a rolling 5 calendar-day window (stay under 4 to avoid PDT flag). SELLs that would be a day trade are blocked when at limit. |
| `TRAIL_ACTIVATION_PCT` | *(from mode)* | Trailing stop activates when price is this % above entry |
| `TRAIL_PCT` | *(from mode)* | Once active, sell if price falls this % from the running high |
| `ADX_STRONG_TREND_THRESHOLD` | *(from mode)* | Minimum ADX value to consider a trend strong enough to trade |
| `NEAR_UPPER_BAND_TOLERANCE` | *(from mode)* | Treat price as "near upper band" when within this % below the BB upper band |
| `SIMILAR_TO_YESTERDAY_PCT` | *(from mode)* | Block entries when today's move vs prior close is less than this % |
| `RISK_PCT_PER_TRADE` | *(from mode)* | Risk this fraction of equity per trade; set to `None` for fixed qty=1 |
| `MAX_POSITION_PCT_EQUITY` | *(from mode)* | Cap position value at this fraction of equity per symbol |
| `MIN_SHARES` / `MAX_SHARES` | *(from mode)* | Clamp share count when using qty-based sizing (not notional) |
| `NOTIONAL_PER_TRADE` | *(from mode)* | When set, each BUY is this many **dollars** (fractional shares); Alpaca min $1. Set to `None` for qty-based sizing. **Note:** `None` is the safer default for most users (enables ATR/risk-based position sizing). Fixed small notionals increase % transaction costs on round trips. |

---

## What’s Implemented

### Data pipeline

- **`data_fetch.fetch_and_store(symbol)`**  
  Fetches daily candles via `data_providers` (Yahoo Finance primary, Finnhub fallback) and upserts into DuckDB table `trends` (symbol, timestamp, open, high, low, close, volume). Timestamps are normalized to epoch seconds and invalid rows are dropped before upsert.

- **`data_providers`**  
  Unified provider layer. `get_daily_candles_with_failover(symbol)` tries Yahoo Finance first, then Finnhub. `get_intraday_price(symbol)` fetches the latest 1-minute bar from Yahoo Finance for a fresher price at decision time.

- **Data retention and pruning**  
  Each run, after processing all symbols, the bot prunes old rows so the DB doesn’t grow forever. **trends**: rows with `timestamp` older than `TRENDS_RETAIN_DAYS` (default 365) are deleted. **trade_log**: rows with `timestamp_utc` older than `TRADE_LOG_RETAIN_DAYS` (default 30) are deleted. Set either to `0` to disable that prune. Analysis uses the last 300 daily bars; 365 days gives TA-Lib indicators plenty of history. Weekly trade counts need at least 7 days of trade log, so keep `TRADE_LOG_RETAIN_DAYS` ≥ 7.

### Analysis (indicators and signals)

- **`analysis.analyze_trends(symbol, connection=None)`**  
  Loads up to 300 bars from `trends`, computes indicators, and returns a dict of boolean signals and `current_price`. Optional `connection` is for tests with spoofed data.

**Indicators (TA-Lib):**

- **ADX (14)** — trend strength; trade only when ADX > `ADX_STRONG_TREND_THRESHOLD` (default 18).
- **+DI / -DI (14)** — direction and crossovers (bullish when +DI > -DI).
- **Parabolic SAR** (acc=0.02, max=0.20) — trend direction and flip signals.
- **Bollinger Bands (20, 2)** — near upper/lower band, squeeze (< 4% width).
- **RSI (14)** — momentum filter (> 50 for buy, < 35 for dive-bomb).
- **MACD (12, 26, 9)** — momentum confirmation (MACD > signal = bullish).
- **SMA (50)** — longer-term trend (price > SMA_50 for buy bias).
- **Yesterday comparison** — avoid new entries when price is within `SIMILAR_TO_YESTERDAY_PCT` (default 1%) of prior day’s close.

**Longer-term risk (avoid longs):** The bot computes several multi-bar patterns that indicate risky entry and sets `avoid_long` if any are true. No BUY is placed when `avoid_long` is True.

- **Dead-cat bounce** (`dead_cat_bounce`): Over the last 80 bars, a sharp drop (>5% high-to-low) followed by a bounce (price up 1–15% from the low) with the low in the recent half of the window. Often a failed rebound; avoids buying into it.
- **Extended decline** (`extended_decline`): Price is still more than 7% below the 50-bar high. Indicates we’re in a drawdown; avoids catching a falling knife.
- **Volatility spike** (`volatility_spike`): Current ATR(14) is more than 1.5× the ATR from the prior 14-bar window. Entering when volatility has just spiked is risky.

**Signals returned (examples):** `strong_trend`, `uptrend`, `sar_below_price`, `sar_above_price`, `near_upper_band`, `near_lower_band`, `bb_squeeze`, `bullish_crossover`, `bearish_crossover`, `sar_flipped_to_bull`, `sar_flipped_to_bear`, `trending_up_a_lot`, `similar_to_yesterday`, `dive_bombing`, `dead_cat_bounce`, `extended_decline`, `volatility_spike`, `avoid_long`, `current_price`.

**Data quality:** If the latest bar is older than 7 days (production only; allows for weekends and provider delays), analysis returns `None` so the bot doesn’t trade on stale data.

### Trading logic

- **`trading.execute_trade(symbol, analysis)`**  
  Uses the analysis dict to decide whether to submit orders. Order of checks:

1. **Risk limits** — If daily or weekly trade cap is reached, or open positions are at max, no new BUY; log and return.
2. **Stop-loss** — If the symbol has an open position and `current_price <= entry * (1 - STOP_LOSS_PCT)`, submit a market SELL for the full position (unless blocked by PDT), log, and return.
3. **Trailing stop** — If the symbol has an open position and price has been at least `TRAIL_ACTIVATION_PCT` above entry, the bot tracks a running high. If price then falls by `TRAIL_PCT` from that running high, submit a market SELL (unless blocked by PDT). State is persisted in DuckDB so the trail survives restarts.
4. **PDT** — Before any SELL (stop-loss, trailing stop, or signal), if that SELL would be a day trade (same symbol bought and sold today) and the number of day trades in the last 5 calendar days is already at `MAX_DAY_TRADES_IN_5_DAYS`, the SELL is skipped and an alert is sent. Trade log stores `qty` for accurate day-trade counting.
5. **BUY** — Only if all of: `trending_up_a_lot` (composite: strong trend + uptrend + price > SMA50 + MACD > signal + RSI > 50), `sar_below_price`, (bullish crossover or SAR flip to bull, recent or exact), not `similar_to_yesterday`, not `bb_squeeze`, not `avoid_long`, and under `MAX_OPEN_POSITIONS`. If **`NOTIONAL_PER_TRADE`** is set (e.g. `75`), submits a **fractional** market BUY for that many dollars (good for small accounts). Otherwise share quantity is from **position sizing** or fixed 1. Submits market BUY.
6. **SELL (signal)** — If any of: `near_lower_band`, `sar_above_price`, `sar_flipped_to_bear`, `dive_bombing`, `bearish_crossover`, and we have a position (and not blocked by PDT), submit market SELL for full qty.
7. Otherwise log “No signal” with the reasons (e.g. which condition failed).

**Fractional / notional mode:** If `config.NOTIONAL_PER_TRADE` is set (e.g. `75`), each BUY is normally a **dollar amount** (e.g. $75) via Alpaca’s notional order—you get fractional shares. If the stock’s current price is **less than or equal** to that amount (e.g. $45 ≤ $75) and you have the cash, the bot buys **one whole share** instead of the dollar amount. Ideal for small accounts and mixed watchlists (cheap names get whole shares; expensive ones get fractional). The bot caps notional at your buying power and skips if below Alpaca’s $1 minimum. Set to `None` to use share-based sizing.

**Position sizing (qty mode):** When `NOTIONAL_PER_TRADE` is `None`, if `config.RISK_PCT_PER_TRADE` is set (e.g. `0.01` = 1%), the bot sizes each BUY so that the dollar risk per trade equals that fraction of account equity. Stop distance per share is the larger of ATR(14) and `current_price * STOP_LOSS_PCT`. Quantity is rounded down and clamped to `MIN_SHARES`, `MAX_SHARES`, and `MAX_POSITION_PCT_EQUITY` of equity. Set `RISK_PCT_PER_TRADE = None` to use a fixed quantity of 1 share.

**Stop-loss:** Uses Alpaca’s `avg_entry_price` and `analysis['current_price']`. When price is down by at least `STOP_LOSS_PCT` from entry, the position is closed with a market sell. Checked every run before other signals.

**Trade log:** Every submitted order (BUY or SELL) is appended to DuckDB table `trade_log` (timestamp_utc, symbol, side, qty). Daily and weekly limits are computed from this table; `qty` is used for PDT day-trade counting (rolling 5 calendar days). State for the trailing stop (running high per symbol) is stored in `trail_state` and cleared when the position is closed.

### Scheduler and utils

- **`main.py`** — Runs `job()` every `CHECK_INTERVAL_MINUTES`. `job()` skips when the market is closed; first fetches external signals (`signals.fetch_signals`) and executes any signal-driven buys/sells, then for each symbol in `SYMBOLS` calls fetch → analyze → execute_trade. Errors are caught per symbol and per signal.
- **`utils`** — `is_market_open()` (US/Eastern, Mon–Fri 9:30–16:00), logger (file + console), and `logs/` directory created on startup.

---

## Backtesting

Before risking capital, you can replay historical data through the current strategy.

1. **Backfill historical candles** (uses Yahoo Finance + Finnhub for daily candles):

   ```bash
   python backfill.py --start 2025-01-01 --end 2025-03-01 --symbols AAPL,MSFT
   ```

   Data is stored in the `trends_backtest` table by default (so live `trends` is unchanged). Use `--table NAME` to override.

2. **Run the backtest**:

   ```bash
   python backtest.py --start 2025-01-01 --end 2025-03-01 --symbols AAPL,MSFT --capital 100000
   ```

   Output: total return %, max drawdown %, number of trades, win rate, final equity. Optional `--equity-curve path.csv` writes the equity curve for plotting.

The backtest reuses `analyze_trends()` and the same entry/exit rules as live trading (including daily/weekly caps and stop-loss).

---

## Monitoring report

A simple CLI report shows account equity, cash, open positions with unrealized P&L, and recent trades from the trade log:

```bash
python report.py
```

Run on demand or schedule it (e.g. after each bot run or via cron). No separate server required.

---

## Alerts

Optional notifications when a trade is placed or when the bot hits an error:

- **Discord:** Set `DISCORD_WEBHOOK_URL` in `.env` (create a webhook in your server under Server Settings → Integrations → Webhooks). The bot will POST a short message on each BUY/SELL (including stop-loss) and on any per-symbol or prune error.
- **Email (errors only):** Set `ALERT_EMAIL_TO`, `ALERT_EMAIL_FROM`, and `ALERT_EMAIL_SMTP_URL` (e.g. `smtps://user:pass@smtp.example.com:465`) to receive error alerts by email.

Alerts are fire-and-forget; a failing webhook or SMTP does not stop the bot.

---

## Project structure

```
trading-bot/
├── main.py             # Scheduler and main loop
├── backfill.py         # Historical candle backfill for backtesting
├── backtest.py         # Strategy backtest on historical data
├── report.py           # Monitoring report (account, positions, P&L, recent trades)
├── dashboard.py        # FastAPI web dashboard (uvicorn dashboard:app --port 8080)
├── dashboard.html      # Dashboard frontend (served by dashboard.py)
├── alerts.py           # Discord / email alerts on trades and errors
├── config.py           # Symbols, intervals, mode loading, risk settings
├── data_fetch.py       # Providers → DuckDB (fetch_and_store, prune_old_trends)
├── data_providers.py   # Yahoo Finance + Finnhub candle/price fetchers with failover
├── analysis.py         # Indicators and signals (analyze_trends)
├── trading.py          # Order logic, limits, stop-loss (execute_trade, execute_signal_buy/sell)
├── signals.py          # FMP analyst upgrades/downgrades signal feed (fetch_signals)
├── migrations.py       # DB schema init and migrations
├── utils.py            # is_market_open(), logger
├── modes/              # Trading mode parameter files (one per mode)
│   ├── conservative.py
│   ├── moderate.py
│   ├── aggressive.py
│   ├── swing.py
│   └── dormant.py
├── requirements.txt
├── Dockerfile
├── .env.example        # Template for API keys
├── .env                # Your keys (gitignored)
├── logs/
│   └── bot.log
├── tests/
│   ├── conftest.py         # Env setup (sets dummy MOTHERDUCK_TOKEN + TRADING_MODE)
│   ├── helpers.py          # Spoofed OHLC and test DB helpers
│   ├── test_analysis.py    # analyze_trends signal logic
│   ├── test_trading.py     # execute_trade buy/sell/stop-loss/trail logic
│   ├── test_signal_trading.py  # execute_signal_buy / execute_signal_sell
│   ├── test_pdt.py         # PDT day-trade counting and block logic
│   ├── test_signals.py     # fetch_signals FMP filtering and deduplication
│   └── test_modes.py       # Mode file completeness, invariants, and config loading
├── AGENTS.md           # Project memory and architecture (for AI/agents)
└── README.md           # This file
```

---

## Tests

Tests use spoofed data and mocks so no live API keys or real orders are needed.

```bash
pip install -r requirements.txt   # includes pytest
python -m pytest tests/ -v
```

- **`test_analysis.py`** — Downtrend produces no buy signal; insufficient data returns `None`; return dict has all keys trading expects.
- **`test_trading.py`** — No order when analysis is missing or no strong trend; no BUY in downtrend; BUY when all conditions met; SELL when conditions met and position exists; no SELL when no position; stop-loss sells when price below threshold; trailing stop activates and fires correctly.
- **`test_signal_trading.py`** — `execute_signal_buy` respects daily/weekly/position caps, double-buy guard, notional sizing, and buying-power floor; `execute_signal_sell` enforces 24-hour hold, PDT guard, and position check.
- **`test_pdt.py`** — `_count_day_trades_in_last_5_days` SQL/Python logic (empty log, buy-only, buy+sell same day, cross-day, 5-day window cutoff, partial close, multiple sells); `_would_sell_be_day_trade` and `_should_block_sell_pdt` gate logic. Uses a real temp DuckDB file.
- **`test_signals.py`** — `fetch_signals` filtering: no API key, HTTP errors, non-list responses, all buy/sell action types, all grade strings, case-insensitive matching, wrong date, deduplication, and `publishedDate` fallback.
- **`test_modes.py`** — Every mode file has all required keys with positive values; aggressive > moderate > conservative ordering of caps; swing trail is wider than all other modes; dormant blocks all trades; `config.py` loads the correct mode and rejects invalid ones; per-mode trading behavior (notional sizing, stop distances, trail activation).

---

## Strategy summary

**Entry (all required):** Strong trend (ADX > `ADX_STRONG_TREND_THRESHOLD`, default 18), uptrend (+DI > -DI), price above SAR, fresh bullish signal (crossover or SAR flip, recent or exact), MACD > signal, RSI > 50, price > SMA(50), not similar to yesterday, no BB squeeze, and under position/trade limits. Alternatively, an analyst upgrade from the FMP signal feed triggers a buy without TA confirmation (still subject to risk/position limits).

**Exit (any):** Price below SAR or SAR flip to bear, near lower band, dive-bombing (downtrend + RSI < 35 + sharp drop), bearish +DI/-DI crossover, or **stop-loss** (price down ≥ `STOP_LOSS_PCT` from average entry).

---

## Deployment

The bot is a **long-running process**: it loops every 60 minutes and only does work during US market hours. You need a machine that stays on (or a server that runs 24/7).

### Where to run

| Option | Best for |
|--------|----------|
| **Your computer** | Testing; run `python main.py` in a terminal (or `nohup python main.py &` to keep it running after you close the shell). |
| **VPS / cloud VM** | Always-on without leaving your PC on. Examples: [DigitalOcean](https://www.digitalocean.com/), [Linode](https://www.linode.com/), [Hetzner](https://www.hetzner.com/), AWS EC2, etc. |
| **Docker (any host)** | Same environment everywhere; good for VPS or a home server. |

### Should you containerize?

- **Yes** if you want a single, reproducible way to run the bot (same Python, TA-Lib, deps) on any host. Use the included `Dockerfile`.
- **No** if you’re fine with a venv on one machine (e.g. your laptop or a single VPS). Just run `python main.py` there.

### How to run

**1. Local (no Docker)**  
From the project root with venv activated:

```bash
python main.py
```

Runs forever; schedule fires every 60 minutes. Logs go to `logs/bot.log` and the console.

**2. Docker**  
Build and run; mount the project dir so the container uses your `.env`, and `trends.db` and `logs/` persist on the host:

```bash
docker build -t trading-bot .
docker run -d --name trading-bot --env-file .env -v "$(pwd):/app" -w /app trading-bot
```

To view logs: `docker logs -f trading-bot`. To stop: `docker stop trading-bot`.

**3. VPS (systemd)**  
On a Linux server, run the bot as a service so it restarts on reboot:

- Clone the repo, create a venv, install deps, add `.env`.
- Create a systemd unit (e.g. `/etc/systemd/system/trading-bot.service`) that runs `python main.py` from the project dir with the venv’s Python.
- `systemctl enable --now trading-bot`.

**4. Cron (alternative)**  
Instead of a long-running process, you can run the job once every 10 minutes during market hours:

- Add a one-shot entrypoint, e.g. `python -c "from main import job; job()"`, or a small script that calls `job()` then exits.
- In crontab, run that every 60 minutes (and optionally only between 9:30 and 16:00 ET on weekdays).

The current `main.py` is built as a daemon (infinite loop); the Dockerfile and “run the bot” instructions assume that model. For cron, you’d add a separate script or flag that runs `job()` once and exits.

---

## Disclaimer

This bot is for education and paper trading. Use at your own risk. Past behavior and tests do not guarantee future results. Paper trading uses simulated money; switching to live trading can have real financial impact. Ensure you understand the strategy, risk limits, and broker terms before using real capital.
