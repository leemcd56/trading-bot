import os
from dotenv import load_dotenv

load_dotenv()

SYMBOLS = ['AAPL', 'TSLA', 'GOOG', 'MSFT']  # your watchlist
CHECK_INTERVAL_MINUTES = 60  # with daily candles, checking once per hour is plenty

# Database: force MotherDuck (no local DuckDB fallback)
_motherduck_token = os.getenv("MOTHERDUCK_TOKEN")
if not _motherduck_token:
    raise RuntimeError(
        "MOTHERDUCK_TOKEN is not set. This project is configured to use MotherDuck "
        "only and will not fall back to a local DuckDB file. "
        "Set MOTHERDUCK_TOKEN in your environment or .env."
    )
DB_PATH = f"md:?motherduck_token={_motherduck_token}"

# Data retention (prune older rows to keep DB small)
TRENDS_RETAIN_DAYS = 365      # keep this many days of daily candles per symbol
TRADE_LOG_RETAIN_DAYS = 30    # keep this many days of trade log (for daily/weekly counts we need 7+)

# Risk limits (no new orders when at cap)
MAX_DAILY_TRADES = 3   # max new orders per calendar day
MAX_WEEKLY_TRADES = 8  # max new orders in rolling 7 calendar days
MAX_OPEN_POSITIONS = 4  # max symbols held at once (across SYMBOLS)

# Stop-loss: sell if position is down this much from average entry (e.g. 0.05 = 5%)
STOP_LOSS_PCT = 0.05

# PDT: max day trades in a rolling 5 calendar-day window (stay under 4 to avoid PDT flag)
MAX_DAY_TRADES_IN_5_DAYS = 3

# Trailing stop: activate when price is this much above entry (e.g. 0.05 = 5%)
TRAIL_ACTIVATION_PCT = 0.05
# Once active, sell if price falls this much from running high (e.g. 0.04 = 4%)
TRAIL_PCT = 0.04

# Position sizing: risk this fraction of equity per trade (e.g. 0.01 = 1%)
# Set to None to use fixed qty=1 instead of risk-based sizing (ignored when NOTIONAL_PER_TRADE is set)
RISK_PCT_PER_TRADE = 0.01
# Cap position value at this fraction of equity per symbol (e.g. 0.10 = 10%)
MAX_POSITION_PCT_EQUITY = 0.10
# Min/max shares per order (when using qty-based sizing, not notional)
MIN_SHARES = 1
MAX_SHARES = 100

# Fractional / small-account mode: when set, each BUY is this many dollars (notional) instead of shares.
# Example: 75 = buy $75 of the symbol per trade (fractional shares). Alpaca minimum is $1.
# Set to None to use qty-based sizing above.
NOTIONAL_PER_TRADE = 75

# Entry tuning (conservative, but not overly restrictive)
# Treat price as "near upper band" when within this fraction below BB upper band.
# Example: 0.015 = within 1.5% of upper band.
NEAR_UPPER_BAND_TOLERANCE = 0.015
# Block entries only when today's move vs yesterday is very small.
# Example: 0.01 = less than 1% move is considered "similar".
SIMILAR_TO_YESTERDAY_PCT = 0.01
