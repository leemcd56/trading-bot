SYMBOLS = ['AAPL', 'TSLA', 'GOOG', 'MSFT']  # your watchlist
CHECK_INTERVAL_MINUTES = 10
DB_PATH = 'trends.db'

# Data retention (prune older rows to keep DB small)
TRENDS_RETAIN_DAYS = 7        # keep this many days of 1-min candles per symbol
TRADE_LOG_RETAIN_DAYS = 30    # keep this many days of trade log (for daily/weekly counts we need 7+)

# Risk limits (no new orders when at cap)
MAX_DAILY_TRADES = 3   # max new orders per calendar day
MAX_WEEKLY_TRADES = 8  # max new orders in rolling 7 calendar days
MAX_OPEN_POSITIONS = 4  # max symbols held at once (across SYMBOLS)

# Stop-loss: sell if position is down this much from average entry (e.g. 0.05 = 5%)
STOP_LOSS_PCT = 0.05
