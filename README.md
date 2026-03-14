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
- **MotherDuck (optional):** Set `MOTHERDUCK_TOKEN` to use [MotherDuck](https://motherduck.com) hosted DuckDB instead of a local `trends.db` file. Data (candles, trade log) then lives in the cloud and survives redeploys or machine loss.

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
| `DB_PATH` | `'trends.db'` or MotherDuck | Set `MOTHERDUCK_TOKEN` in `.env` to use hosted DuckDB (persistent cloud storage); otherwise uses local `trends.db` |
| `TRENDS_RETAIN_DAYS` | `7` | Keep this many days of candle data; older rows are pruned each run |
| `TRADE_LOG_RETAIN_DAYS` | `30` | Keep this many days of trade log; older rows pruned (weekly count needs 7+) |
| `MAX_DAILY_TRADES` | `3` | Max new orders in rolling 24 hours |
| `MAX_WEEKLY_TRADES` | `8` | Max new orders in rolling 7 days |
| `MAX_OPEN_POSITIONS` | `4` | Max symbols held at once (no new BUY above this) |
| `STOP_LOSS_PCT` | `0.05` | Sell if position is down 5% from average entry (e.g. `0.03` = 3%) |
| `MAX_DAY_TRADES_IN_5_DAYS` | `3` | PDT: max day trades in a rolling 5 calendar-day window (stay under 4 to avoid PDT flag). SELLs that would be a day trade are blocked when at limit. |
| `TRAIL_ACTIVATION_PCT` | `0.05` | Trailing stop activates when price is 5% above entry |
| `TRAIL_PCT` | `0.04` | Once trailing stop is active, sell if price falls 4% from the running high since activation |
| `RISK_PCT_PER_TRADE` | `0.01` | Risk this fraction of equity per trade (1%); set to `None` for fixed qty=1 |
| `MAX_POSITION_PCT_EQUITY` | `0.10` | Cap position value at 10% of equity per symbol |
| `MIN_SHARES` / `MAX_SHARES` | `1` / `100` | Clamp share count when using qty-based sizing (not notional) |
| `NOTIONAL_PER_TRADE` | `75` | When set, each BUY is this many **dollars** (fractional shares); Alpaca min $1. Set to `None` for qty-based sizing. Good for small accounts (e.g. $50–$75 per trade). |

---

## What’s Implemented

### Data pipeline

- **`data_fetch.fetch_and_store(symbol)`**  
  Fetches 1-minute candles from Finnhub for the last ~2000 minutes, upserts into DuckDB table `trends` (symbol, timestamp, open, high, low, close, volume). Retries on 429 (rate limit) and 5xx with exponential backoff.

- **Data retention and pruning**  
  Each run, after processing all symbols, the bot prunes old rows so the DB doesn’t grow forever. **trends**: rows with `timestamp` older than `TRENDS_RETAIN_DAYS` (default 7) are deleted. **trade_log**: rows with `timestamp_utc` older than `TRADE_LOG_RETAIN_DAYS` (default 30) are deleted. Set either to `0` to disable that prune. Analysis only uses the last 300 bars (~5 h); 7 days of 1-min data is plenty. Weekly trade counts need at least 7 days of trade log, so keep `TRADE_LOG_RETAIN_DAYS` ≥ 7.

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

**Longer-term risk (avoid longs):** The bot computes several multi-bar patterns that indicate risky entry and sets `avoid_long` if any are true. No BUY is placed when `avoid_long` is True.

- **Dead-cat bounce** (`dead_cat_bounce`): Over the last 80 bars, a sharp drop (>5% high-to-low) followed by a bounce (price up 1–15% from the low) with the low in the recent half of the window. Often a failed rebound; avoids buying into it.
- **Extended decline** (`extended_decline`): Price is still more than 7% below the 50-bar high. Indicates we’re in a drawdown; avoids catching a falling knife.
- **Volatility spike** (`volatility_spike`): Current ATR(14) is more than 1.5× the ATR from the prior 14-bar window. Entering when volatility has just spiked is risky.

**Signals returned (examples):** `strong_trend`, `uptrend`, `sar_below_price`, `sar_above_price`, `near_upper_band`, `near_lower_band`, `bb_squeeze`, `bullish_crossover`, `bearish_crossover`, `sar_flipped_to_bull`, `sar_flipped_to_bear`, `trending_up_a_lot`, `similar_to_yesterday`, `dive_bombing`, `dead_cat_bounce`, `extended_decline`, `volatility_spike`, `avoid_long`, `current_price`.

**Data quality:** If the latest bar is older than 45 minutes (production only), analysis returns `None` so the bot doesn’t trade on stale data.

### Trading logic

- **`trading.execute_trade(symbol, analysis)`**  
  Uses the analysis dict to decide whether to submit orders. Order of checks:

1. **Risk limits** — If daily or weekly trade cap is reached, or open positions are at max, no new BUY; log and return.
2. **Stop-loss** — If the symbol has an open position and `current_price <= entry * (1 - STOP_LOSS_PCT)`, submit a market SELL for the full position (unless blocked by PDT), log, and return.
3. **Trailing stop** — If the symbol has an open position and price has been at least `TRAIL_ACTIVATION_PCT` above entry, the bot tracks a running high. If price then falls by `TRAIL_PCT` from that running high, submit a market SELL (unless blocked by PDT). State is persisted in DuckDB so the trail survives restarts.
4. **PDT** — Before any SELL (stop-loss, trailing stop, or signal), if that SELL would be a day trade (same symbol bought and sold today) and the number of day trades in the last 5 calendar days is already at `MAX_DAY_TRADES_IN_5_DAYS`, the SELL is skipped and an alert is sent. Trade log stores `qty` for accurate day-trade counting.
5. **BUY** — Only if all of: `strong_trend`, `trending_up_a_lot`, `near_upper_band`, `sar_below_price`, (bullish crossover or SAR flip to bull), not `similar_to_yesterday`, not `bb_squeeze`, not `avoid_long`, and under `MAX_OPEN_POSITIONS`. If **`NOTIONAL_PER_TRADE`** is set (e.g. `75`), submits a **fractional** market BUY for that many dollars (good for small accounts). Otherwise share quantity is from **position sizing** or fixed 1. Submits market BUY.
6. **SELL (signal)** — If any of: `near_lower_band`, `sar_above_price`, `sar_flipped_to_bear`, `dive_bombing`, `bearish_crossover`, and we have a position (and not blocked by PDT), submit market SELL for full qty.
7. Otherwise log “No signal” with the reasons (e.g. which condition failed).

**Fractional / notional mode:** If `config.NOTIONAL_PER_TRADE` is set (e.g. `75`), each BUY is normally a **dollar amount** (e.g. $75) via Alpaca’s notional order—you get fractional shares. If the stock’s current price is **less than or equal** to that amount (e.g. $45 ≤ $75) and you have the cash, the bot buys **one whole share** instead of the dollar amount. Ideal for small accounts and mixed watchlists (cheap names get whole shares; expensive ones get fractional). The bot caps notional at your buying power and skips if below Alpaca’s $1 minimum. Set to `None` to use share-based sizing.

**Position sizing (qty mode):** When `NOTIONAL_PER_TRADE` is `None`, if `config.RISK_PCT_PER_TRADE` is set (e.g. `0.01` = 1%), the bot sizes each BUY so that the dollar risk per trade equals that fraction of account equity. Stop distance per share is the larger of ATR(14) and `current_price * STOP_LOSS_PCT`. Quantity is rounded down and clamped to `MIN_SHARES`, `MAX_SHARES`, and `MAX_POSITION_PCT_EQUITY` of equity. Set `RISK_PCT_PER_TRADE = None` to use a fixed quantity of 1 share.

**Stop-loss:** Uses Alpaca’s `avg_entry_price` and `analysis['current_price']`. When price is down by at least `STOP_LOSS_PCT` from entry, the position is closed with a market sell. Checked every run before other signals.

**Trade log:** Every submitted order (BUY or SELL) is appended to DuckDB table `trade_log` (timestamp_utc, symbol, side, qty). Daily and weekly limits are computed from this table; `qty` is used for PDT day-trade counting (rolling 5 calendar days). State for the trailing stop (running high per symbol) is stored in `trail_state` and cleared when the position is closed.

### Scheduler and utils

- **`main.py`** — Runs `job()` every `CHECK_INTERVAL_MINUTES`. `job()` skips when the market is closed; for each symbol it calls fetch → analyze → execute_trade and logs errors per symbol.
- **`utils`** — `is_market_open()` (US/Eastern, Mon–Fri 9:30–16:00), logger (file + console), and `logs/` directory created on startup.

---

## Backtesting

Before risking capital, you can replay historical data through the current strategy.

1. **Backfill historical candles** (uses Finnhub; free tier may limit 1-min history):

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
├── main.py           # Scheduler and main loop
├── backfill.py       # Historical candle backfill for backtesting
├── backtest.py       # Strategy backtest on historical data
├── report.py         # Monitoring report (account, positions, P&L, recent trades)
├── alerts.py         # Discord / email alerts on trades and errors
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

## Deployment

The bot is a **long-running process**: it loops every 10 minutes and only does work during US market hours. You need a machine that stays on (or a server that runs 24/7).

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

Runs forever; schedule fires every 10 minutes. Logs go to `logs/bot.log` and the console.

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
- In crontab, run that every 10 minutes (and optionally only between 9:30 and 16:00 ET on weekdays).

The current `main.py` is built as a daemon (infinite loop); the Dockerfile and “run the bot” instructions assume that model. For cron, you’d add a separate script or flag that runs `job()` once and exits.

---

## Disclaimer

This bot is for education and paper trading. Use at your own risk. Past behavior and tests do not guarantee future results. Paper trading uses simulated money; switching to live trading can have real financial impact. Ensure you understand the strategy, risk limits, and broker terms before using real capital.
