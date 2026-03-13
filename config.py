import os
from dotenv import load_dotenv

load_dotenv()

SYMBOLS = ['AAPL', 'TSLA', 'GOOG', 'MSFT']  # your watchlist
CHECK_INTERVAL_MINUTES = 10

# Database: local file or MotherDuck (hosted DuckDB) for persistent cloud storage
_motherduck_token = os.getenv("MOTHERDUCK_TOKEN")
DB_PATH = (
    f"md:?motherduck_token={_motherduck_token}"
    if _motherduck_token
    else "trends.db"
)

# Data retention (prune older rows to keep DB small)
TRENDS_RETAIN_DAYS = 7        # keep this many days of 1-min candles per symbol
TRADE_LOG_RETAIN_DAYS = 30    # keep this many days of trade log (for daily/weekly counts we need 7+)

# Risk limits (no new orders when at cap)
MAX_DAILY_TRADES = 3   # max new orders per calendar day
MAX_WEEKLY_TRADES = 8  # max new orders in rolling 7 calendar days
MAX_OPEN_POSITIONS = 4  # max symbols held at once (across SYMBOLS)

# Stop-loss: sell if position is down this much from average entry (e.g. 0.05 = 5%)
STOP_LOSS_PCT = 0.05

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
