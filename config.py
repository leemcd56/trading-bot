import os
import importlib
from dotenv import load_dotenv

load_dotenv()

_symbols_env = os.getenv("WATCH_SYMBOLS", "")
SYMBOLS = (
    [s.strip().upper() for s in _symbols_env.split(",") if s.strip()]
    if _symbols_env.strip()
    else ["AAPL", "TSLA", "GOOG", "MSFT"]
)
CHECK_INTERVAL_MINUTES = 60          # TA loop: once per hour is plenty with daily candles
FMP_CHECK_INTERVAL_MINUTES = 30     # FMP signal check: every 30 min to catch intraday analyst actions early

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

# PDT: max day trades in a rolling 5 calendar-day window (stay under 4 to avoid PDT flag).
# Not mode-specific — this is a regulatory limit.
MAX_DAY_TRADES_IN_5_DAYS = 3

# ─── Trading mode ────────────────────────────────────────────────────────────────
# Set TRADING_MODE in .env (or the environment) to one of:
#   conservative  — infrequent, high-conviction trades; build a nest egg
#   moderate      — balanced risk/reward (default)
#   aggressive    — high-frequency day trading; maximize activity and size
#   swing         — ride multi-day trends with wide stops; let winners run
#   dormant       — analysis and alerts only; no orders submitted
#
# Each mode lives in modes/<name>.py and exports a PARAMS dict.
# Individual overrides (MAX_DAILY_TRADES, MAX_WEEKLY_TRADES) are still accepted
# as env vars and take priority over the mode's defaults.

_VALID_MODES = ("conservative", "moderate", "aggressive", "swing", "dormant")
TRADING_MODE = os.getenv("TRADING_MODE", "moderate").lower().strip()
if TRADING_MODE not in _VALID_MODES:
    raise RuntimeError(
        f"Invalid TRADING_MODE '{TRADING_MODE}'. Must be one of: {', '.join(_VALID_MODES)}"
    )

_mode = importlib.import_module(f"modes.{TRADING_MODE}").PARAMS

# ─── Risk limits ─────────────────────────────────────────────────────────────────
# MAX_DAILY_TRADES and MAX_WEEKLY_TRADES can be overridden via env vars; all others
# come directly from the selected mode.

MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", str(_mode["MAX_DAILY_TRADES"])))
MAX_WEEKLY_TRADES = int(os.getenv("MAX_WEEKLY_TRADES", str(_mode["MAX_WEEKLY_TRADES"])))
MAX_OPEN_POSITIONS = _mode["MAX_OPEN_POSITIONS"]

# Stop-loss / trailing stop
STOP_LOSS_PCT = _mode["STOP_LOSS_PCT"]
TRAIL_ACTIVATION_PCT = _mode["TRAIL_ACTIVATION_PCT"]
TRAIL_PCT = _mode["TRAIL_PCT"]

# Position sizing
RISK_PCT_PER_TRADE = _mode["RISK_PCT_PER_TRADE"]
MAX_POSITION_PCT_EQUITY = _mode["MAX_POSITION_PCT_EQUITY"]
MIN_SHARES = _mode["MIN_SHARES"]
MAX_SHARES = _mode["MAX_SHARES"]
NOTIONAL_PER_TRADE = _mode["NOTIONAL_PER_TRADE"]

# Entry filters
ADX_STRONG_TREND_THRESHOLD = _mode["ADX_STRONG_TREND_THRESHOLD"]
NEAR_UPPER_BAND_TOLERANCE = _mode["NEAR_UPPER_BAND_TOLERANCE"]
SIMILAR_TO_YESTERDAY_PCT = _mode["SIMILAR_TO_YESTERDAY_PCT"]
