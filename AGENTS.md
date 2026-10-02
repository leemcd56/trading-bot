# AGENTS.md – Trading Bot Project Memory & Architecture

**Project Name**: Pan's Algorithmic Trading Bot (🐾)  
**Goal**: Build a low-frequency, trend-following stock trading system in Python  
**Style**: Conservative – infrequent trades, strong trend filters, paper trading first  
**Current Date in Conversation**: March 2026  
**Brokerage**: Alpaca (paper mode first)  
**Data Provider**: Yahoo Finance (daily via yfinance) + Finnhub fallback  
**Database**: DuckDB / MotherDuck (for backtesting & logs; live trading can run without it)  
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
├── data_fetch.py           # fetch_and_store(symbol) → providers → DuckDB (for backtesting / history)
├── backfill.py             # Historical backfill into trends_backtest for backtesting
├── backtest.py             # Backtest engine (reuses analyze_trends + trading rules)
├── analysis.py             # analyze_trends(symbol) / analyze_trends_from_providers(symbol) ← ALL indicators, returns atr_14 for sizing
├── trading.py              # execute_trade(symbol, analysis) ← Alpaca, position sizing
├── utils.py                # is_market_open(), logger, helpers
├── .env                    # API keys (gitignore!)
├── requirements.txt
├── trends.db               # DuckDB file (local dev only; MotherDuck in prod)
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

**Buy (Long) Condition** (many gates are now mode-dependent via the selected TRADING_MODE)

- Strong trend: `ADX > ADX_STRONG_TREND_THRESHOLD` (25 conservative, 18 moderate, 14 aggressive)
- Uptrend direction: `+DI > -DI`
- Price above Parabolic SAR (`sar_below_price`)
- (Optional, conservative only) Price above BB middle band + near/touching upper band (`REQUIRE_NEAR_UPPER_BAND`)
- (Optional) Fresh bullish signal: `bullish_crossover` **or** `sar_flipped_to_bull` (`REQUIRE_BULLISH_TRIGGER`)
- Momentum: `MACD > signal`, `RSI > RSI_ENTRY_THRESHOLD` (55/50/45 depending on mode)
- Not similar to yesterday (threshold from mode)
- No BB squeeze (threshold from mode)
- **Daily compensating filters** (new, the main addition to make aggressive viable on daily bars):
  - `REQUIRE_ADX_RISING`: current ADX > ADX 5 bars ago (trend strengthening)
  - `REQUIRE_VOLUME_CONFIRMATION`: current volume > 20-day SMA volume
  - `LONG_TERM_SMA_PERIOD`: price > SMA(N) when N > 0 (major trend bias)

Conservative/swing use the strictest combination of the above. Moderate is balanced. Aggressive keeps only ADX rising as a minimal daily guardrail and relaxes the rest.

**Sell / Exit Condition** (confirmed exit — bare SAR alone is not enough)

- Hard: `dive_bombing` or `sar_flipped_to_bear`
- Confirmed: `sar_above_price` **and** not uptrend; or `near_lower_band` / `bearish_crossover` with SAR/DI confirmation
- Risk exits always: stop-loss / trailing stop (run even without strong ADX)
- Mode `MIN_HOLD_HOURS` blocks discretionary TA sells only (not stops): aggressive 0h, moderate/conservative 24h, swing 48h

**Regime-switching counter-strategy: mean-reversion** (optional, OFF by default in every mode)

- When a symbol is NOT trending (`strong_trend` false), the bot used to just sit out. It can now optionally trade short-term RSI/Bollinger-Band mean-reversion in that chop instead, via `trading._try_mean_reversion` (called from `execute_trade`'s non-trending branch).
- Regime gate (`trading._mean_reversion_regime`): `ADX < MEAN_REVERSION_ADX_CEILING` (a ceiling strictly below `ADX_STRONG_TREND_THRESHOLD`, leaving a dead zone where *neither* strategy trades) and none of the `avoid_long` risk heuristics (dead-cat bounce / extended decline / volatility spike) firing — those mean something is wrong, not that the market is calmly ranging.
- Entry: RSI `< MEAN_REVERSION_RSI_OVERSOLD` **and** `near_lower_band`. Exit/take-profit: RSI `> MEAN_REVERSION_RSI_OVERBOUGHT` **or** `near_upper_band` (reached the top of the range).
- Its own, smaller/tighter risk knobs per mode: `MEAN_REVERSION_RISK_PCT_PER_TRADE` (roughly half the trend strategy's) and `MEAN_REVERSION_STOP_LOSS_PCT` (tighter — chop whipsaws more than a trend pulls back). Sized by `trading._compute_mean_reversion_qty`, a fixed-%-stop version of `_compute_buy_qty` (no ATR widening — the stop is a precise short-term target by design).
- Shares every other risk gate with the trend strategy: daily/weekly caps, `MAX_OPEN_POSITIONS`, `MAX_PORTFOLIO_RISK_PCT`, `MAX_POSITION_CORRELATION`, the slippage-capped limit order (`_build_qty_order`), and the AI confirmation gate. Only entry/exit signal, stop distance, and size differ.
- `trading._last_buy_source` tags which strategy opened a position (via the `source` column already recorded in `trade_history`: `"ta"` vs `"mean_reversion"`), so `_try_risk_exit` can apply the right stop % and skip the trailing stop for mean-reversion positions (it takes profit at its own RSI/band target instead — a trailing stop is a "let the winner run" trend concept that fights the mean-reversion thesis). A mean-reversion position that survives into a real trend naturally falls through to trend exit rules once `strong_trend` flips true — not forced to exit early.
- **Off by default in every built-in mode** (`MEAN_REVERSION_ENABLED: False`) — it's newer and less battle-tested than the core trend strategy. Turn it on deliberately per mode (or via env override) once you've paper-traded it and are comfortable with it; see `modes/*.py` for the per-mode values to flip.
- Known simplification: `_portfolio_open_risk_pct` (the portfolio-heat calculation) still uses the global `STOP_LOSS_PCT` for every open position regardless of which strategy opened it, which slightly *overstates* a mean-reversion position's true risk (it actually uses the tighter `MEAN_REVERSION_STOP_LOSS_PCT`) — a safe, conservative approximation, not a bug.

**AI second-opinion gate** (optional, disabled by default)

- The rules engine above still forms its own BUY/SELL assumption first — nothing about the TA gates changes.
- Only once a TA-driven BUY or SELL has passed every gate (and the daily/weekly/open-position caps, for BUYs) does `ai_review.confirm_trade()` hand that specific proposed trade, plus the indicator snapshot, to an LLM (Anthropic Messages API) to CONFIRM or VETO. The AI cannot originate a trade, pick a different symbol/side, or turn a HOLD into a trade.
- Stop-loss and trailing-stop exits (`_try_risk_exit`) and the FMP signal path (`execute_signal_buy`/`execute_signal_sell`) intentionally bypass this gate — safety exits and the external oracle path must never be delayed or blocked by an extra network call.
- Enable with `AI_CONFIRMATION_ENABLED=true` and `ANTHROPIC_API_KEY` in `.env`; optional `AI_CONFIRMATION_MODEL` (default `claude-sonnet-5`). Fails closed (VETO) on any error, timeout, or missing key.

## Important Files – Where the Logic Lives

- **`analysis.py`**  
  → Contains the massive `analyze_trends(symbol)` function (reads from DuckDB / MotherDuck)  
  → Also `analyze_trends_from_providers(symbol)`, which fetches candles directly from providers (yfinance/Finnhub) and runs the same indicator stack **without touching the DB**  
  → All TA-Lib calls, DataFrame manipulations, boolean flags  
  → Returns rich dict with every signal/metric (including raw `adx`, `rsi_14`, `macd`, etc. for the AI review gate)

- **`trading.py`**  
  → `execute_trade(symbol, analysis)`  
  → Alpaca TradingClient initialization  
  → Buy/sell order submission logic: qty-based entries/discretionary exits use a marketable limit order capped at `MAX_SLIPPAGE_PCT` from the decision-time price (`_build_qty_order`); notional/fractional orders stay market orders (Alpaca only supports notional on MarketOrderRequest); stop-loss/trailing-stop exits (`_try_risk_exit`) always stay plain market orders (guaranteed execution over price). Fill slippage past the cap is logged + alerted via `_log_fill_slippage`, mainly relevant to the unprotected notional path.  
  → Calls `ai_review.confirm_trade()` before placing a TA-driven BUY/SELL order (see AI second-opinion gate above)

- **`ai_review.py`**  
  → `confirm_trade(symbol, action, analysis, mode)` — the optional LLM confirm/veto gate described above

- **`data_fetch.py`**  
  → `fetch_and_store(symbol)`  
  → Uses unified providers (currently yfinance daily candles + Finnhub fallback)  
  → Upserts into DuckDB / MotherDuck table `trends` for **historical storage & backtesting**, not required for live trade decisions

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
- **Portfolio-level risk cap** — Implemented: `config.MAX_PORTFOLIO_RISK_PCT` caps total equity at risk across ALL open positions at once (sum of `qty * entry_price * STOP_LOSS_PCT`, computed in `trading._portfolio_open_risk_pct`), checked in `trading._entry_caps_allow_buy` alongside the daily/weekly/open-position caps. Catches the case `MAX_OPEN_POSITIONS` (a plain headcount) can't: every open slot near its max size/stop distance at the same time.
- **Correlation-aware position limits** — Implemented: `config.MAX_POSITION_CORRELATION` (per-mode: conservative 0.6 / moderate & swing 0.75 / aggressive 0.9) refuses a new BUY whose daily-return correlation with an already-held symbol (over `CORRELATION_LOOKBACK_DAYS`, from the `trends` table) meets or exceeds the threshold — `trading._max_correlation_with_held`, checked last in `trading._entry_caps_allow_buy`. Deliberately best-effort/fail-open: unlike the daily/weekly/open-position/heat checks above it, missing or too-thin history (`CORRELATION_MIN_SAMPLES`, default 20 bars) just excludes that symbol pair rather than blocking the trade — this is a diversification check, not a hard safety rail, and the default `SYMBOLS` watchlist (AAPL/TSLA/GOOG/MSFT) won't always have deep history for every pair.
- **Simple monitoring** — Implemented: `report.py` prints account equity, positions, unrealized P&L, daily/weekly trade counts, and recent trade log. Run on demand or on a schedule.
- **Benchmark-relative reporting** — Implemented: `report.fetch_benchmark_comparison()` compares the bot's return since its first recorded equity snapshot (`portfolio_snapshots`, earliest `label='open'` row — i.e. since the bot started, not a rolling window) against a simple buy-and-hold of `BENCHMARK_SYMBOL` (`.env`, default `SPY`) over the same span — the honest check on whether active trading is earning its complexity over the simplest possible alternative. Shown in both `print_report()` (on-demand text report) and `send_eod_summary()` (daily Discord embed, as a 📐 line). Returns `None` (and both callers render a graceful "not enough data yet" message) when there's no snapshot yet, the account can't be read, or the benchmark's price history can't be fetched — all data gaps, never a fabricated number.
- **Alerts** — Implemented: `alerts.py` sends Discord webhook messages on each trade (BUY/SELL/stop-loss) and on errors; optional email for errors only. Set `DISCORD_WEBHOOK_URL` (and optionally `ALERT_EMAIL_*`) in `.env`.
- **Dead-man's-switch heartbeat** — Implemented: `alerts.send_heartbeat()` pings an external uptime-monitoring URL (`HEARTBEAT_URL` in `.env`, e.g. a free [healthchecks.io](https://healthchecks.io) check) once per `main.ta_job()` cycle, wrapped in a `finally` so it fires on every scheduled invocation — including "market closed, nothing to do" — but not if the cycle hangs rather than completing or raising. This is deliberately NOT another bot-side Discord/email alert: if the process crashes or hangs, it can't send its own "I'm dead" message, so a third-party service has to notice the *absence* of pings and alert you itself (configure that service's own alerting separately — this just supplies the ping). Without `HEARTBEAT_URL` set, this is a silent no-op and `main.py` logs a startup warning that nothing is monitoring the process.
- **Account-wide circuit breaker** — Implemented, ON by default in every mode (unlike mean-reversion, enabling this only ever makes the bot *more* conservative, so there's no reason to default it off): `trading._circuit_breaker_tripped(equity, open_equity)` compares current equity against today's market-open snapshot (the same `portfolio_snapshots` table `report.snapshot_portfolio("open")` writes to — duplicated read-only in `trading.py` to avoid a circular import) and, once the drawdown reaches `CIRCUIT_BREAKER_DRAWDOWN_PCT` (conservative 3% / moderate & swing 5% / aggressive 8%), blocks all NEW entries via `_entry_caps_allow_buy` for the rest of the day. It deliberately does **not** force-close existing positions — panic-selling into a crash is usually the wrong move, and `_try_risk_exit`'s stop-losses/trailing-stops keep running normally regardless of whether the breaker is tripped. Fails *open* (not tripped) when there's no open-equity baseline yet (e.g. the first few minutes after startup, before `open_snapshot_job` has run — `main.py` now runs it immediately at startup, before `ta_job()`, to close that gap) — a missing baseline is a data gap, not evidence something is wrong. Trip alerts are deduplicated to once per calendar day (`circuit_breaker_trips` table) so a tripped breaker doesn't spam Discord every cycle for the rest of the day.
- **Execution quality** — Implemented: `config.MAX_SLIPPAGE_PCT` caps qty-based orders at a marketable limit price (`trading._build_qty_order`); stop-loss/trailing-stop and notional/fractional orders are exempt (guaranteed execution, and an Alpaca API constraint, respectively — see `trading.py` above). Realized slippage on the unprotected notional path is logged/alerted via `trading._log_fill_slippage`. Not implemented: pre-trade spread/liquidity checks, and no awareness of open/close volatility windows.
- **Regime switching** — Implemented, OFF by default: a mean-reversion counter-strategy trades RSI/band bounces when `strong_trend` is false and ADX says the market is genuinely choppy, instead of sitting out every non-trending stretch. See "Regime-switching counter-strategy" above for the full gate/signal/risk breakdown. Turn on deliberately per mode via `MEAN_REVERSION_ENABLED` once comfortable after paper trading it.

## Quick Commands Reminder

```bash
# Activate venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows

# Run bot
python main.py

# Install missing deps
pip install -r requirements.txt