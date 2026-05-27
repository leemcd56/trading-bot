import os
import importlib
import logging
from dotenv import load_dotenv

load_dotenv()

_log = logging.getLogger(__name__)

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

# ─── Safe fallback defaults (defensive loading) ──────────────────────────────────
# These are used ONLY when a mode file is missing a required key.
# They are deliberately conservative-leaning so that a missing or incomplete
# mode definition cannot silently cause dangerous behavior (especially in
# aggressive mode). Built-in mode files are expected to be complete (tests
# enforce this), but the fallbacks protect against manual edits, new modes,
# or accidental deletions.
#
# Common-sense philosophy for fallbacks:
#   - High ADX bar (prefer missing trades over bad ones)
#   - Wider stops / later trail activation (give positions room)
#   - Low trade frequency caps
#   - Small risk per trade
#   - NOTIONAL_PER_TRADE=None → enables ATR-based risk sizing in trading.py
#     (fixed small notionals like $75 amplify % costs on round trips)
_SAFE_FALLBACKS = {
    # Trade frequency caps — conservative activity
    "MAX_DAILY_TRADES": 1,
    "MAX_WEEKLY_TRADES": 3,
    "MAX_OPEN_POSITIONS": 2,

    # Stop-loss / trailing stop — give positions breathing room
    "STOP_LOSS_PCT": 0.07,
    "TRAIL_ACTIVATION_PCT": 0.10,
    "TRAIL_PCT": 0.06,

    # Entry gate strictness — only strong, confirmed trends
    "ADX_STRONG_TREND_THRESHOLD": 22,
    "NEAR_UPPER_BAND_TOLERANCE": 0.02,
    "SIMILAR_TO_YESTERDAY_PCT": 0.015,
    # New mode-aware entry filters (conservative-leaning safe defaults)
    "RSI_ENTRY_THRESHOLD": 55,
    "REQUIRE_BULLISH_TRIGGER": True,
    "BB_SQUEEZE_MAX_WIDTH_PCT": 0.05,
    "REQUIRE_NEAR_UPPER_BAND": True,

    # Daily-bar compensating filters (safe conservative defaults — very effective on daily data)
    "REQUIRE_ADX_RISING": True,
    "REQUIRE_VOLUME_CONFIRMATION": True,
    "LONG_TERM_SMA_PERIOD": 200,

    # Position sizing — small risk; prefer ATR-based over fixed tiny notionals
    "RISK_PCT_PER_TRADE": 0.005,
    "MAX_POSITION_PCT_EQUITY": 0.08,
    "MIN_SHARES": 1,
    "MAX_SHARES": 100,
    "NOTIONAL_PER_TRADE": None,  # None = use risk/ATR sizing (safer default)
}


def _mode_get(key: str):
    """
    Return a value from the selected mode's PARAMS.
    Falls back to a safe conservative default + warning if the key is missing.
    This guarantees that ADX, trail params, NOTIONAL_PER_TRADE, and all other
    critical risk variables always have common-sense values even if a mode
    file is incomplete.
    """
    if key in _mode:
        return _mode[key]
    default = _SAFE_FALLBACKS.get(key)
    _log.warning(
        "TRADING_MODE=%s PARAMS is missing key '%s'. "
        "Using safe conservative fallback: %s. "
        "Fix this in modes/%s.py (every mode must export all keys listed in "
        "tests/test_modes.py REQUIRED_KEYS).",
        TRADING_MODE, key, default, TRADING_MODE
    )
    return default


# ─── Risk limits ─────────────────────────────────────────────────────────────────
# MAX_DAILY_TRADES and MAX_WEEKLY_TRADES can be overridden via env vars; all others
# come from the selected mode (with safe fallbacks if a key is absent).

_daily_default = _mode_get("MAX_DAILY_TRADES")
_weekly_default = _mode_get("MAX_WEEKLY_TRADES")
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", str(_daily_default)))
MAX_WEEKLY_TRADES = int(os.getenv("MAX_WEEKLY_TRADES", str(_weekly_default)))
MAX_OPEN_POSITIONS = _mode_get("MAX_OPEN_POSITIONS")

# Stop-loss / trailing stop
STOP_LOSS_PCT = _mode_get("STOP_LOSS_PCT")
TRAIL_ACTIVATION_PCT = _mode_get("TRAIL_ACTIVATION_PCT")
TRAIL_PCT = _mode_get("TRAIL_PCT")

# Position sizing
RISK_PCT_PER_TRADE = _mode_get("RISK_PCT_PER_TRADE")
MAX_POSITION_PCT_EQUITY = _mode_get("MAX_POSITION_PCT_EQUITY")
MIN_SHARES = _mode_get("MIN_SHARES")
MAX_SHARES = _mode_get("MAX_SHARES")
NOTIONAL_PER_TRADE = _mode_get("NOTIONAL_PER_TRADE")

# Entry filters
ADX_STRONG_TREND_THRESHOLD = _mode_get("ADX_STRONG_TREND_THRESHOLD")
NEAR_UPPER_BAND_TOLERANCE = _mode_get("NEAR_UPPER_BAND_TOLERANCE")
SIMILAR_TO_YESTERDAY_PCT = _mode_get("SIMILAR_TO_YESTERDAY_PCT")

# New mode-aware entry filters (control signal strictness per trading style)
RSI_ENTRY_THRESHOLD = _mode_get("RSI_ENTRY_THRESHOLD")
REQUIRE_BULLISH_TRIGGER = _mode_get("REQUIRE_BULLISH_TRIGGER")
BB_SQUEEZE_MAX_WIDTH_PCT = _mode_get("BB_SQUEEZE_MAX_WIDTH_PCT")
REQUIRE_NEAR_UPPER_BAND = _mode_get("REQUIRE_NEAR_UPPER_BAND")

# Daily-bar compensating filters (highly effective on daily candles)
REQUIRE_ADX_RISING = _mode_get("REQUIRE_ADX_RISING")
REQUIRE_VOLUME_CONFIRMATION = _mode_get("REQUIRE_VOLUME_CONFIRMATION")
LONG_TERM_SMA_PERIOD = _mode_get("LONG_TERM_SMA_PERIOD")


# ─── Post-load validation (catches broken or reckless combinations) ─────────────
def _validate_mode_params() -> None:
    """
    After loading, validate critical risk parameters.
    Warns on risky-but-usable values; raises on values that would disable
    protection or produce clearly broken behavior.
    Aggressive mode gets extra scrutiny because its explicit values are
    already high-risk; missing keys must never make it worse.
    """
    problems = []

    # ADX too low → admits noise and weak trends
    if ADX_STRONG_TREND_THRESHOLD is not None and ADX_STRONG_TREND_THRESHOLD < 10:
        problems.append(f"ADX_STRONG_TREND_THRESHOLD={ADX_STRONG_TREND_THRESHOLD} is extremely low (<10)")

    # Stops/trails disabled or broken
    if STOP_LOSS_PCT is not None and STOP_LOSS_PCT <= 0:
        problems.append("STOP_LOSS_PCT <= 0 — stop-losses are disabled!")
    if TRAIL_PCT is not None and TRAIL_PCT <= 0:
        problems.append("TRAIL_PCT <= 0 — trailing stops are disabled!")

    # Insane risk sizing
    if RISK_PCT_PER_TRADE is not None and RISK_PCT_PER_TRADE > 0.10:
        problems.append(f"RISK_PCT_PER_TRADE={RISK_PCT_PER_TRADE} (>10% per trade) is extremely aggressive")

    # Nonsensical caps
    if MAX_DAILY_TRADES < 0 or MAX_WEEKLY_TRADES < 0 or MAX_OPEN_POSITIONS < 0:
        problems.append("Negative trade/position caps are invalid")

    # Aggressive-specific warnings (only when the mode is explicitly aggressive)
    if TRADING_MODE == "aggressive":
        if ADX_STRONG_TREND_THRESHOLD is not None and ADX_STRONG_TREND_THRESHOLD < 12:
            problems.append(
                "aggressive mode with ADX < 12 — this will overtrade weak/choppy trends; "
                "consider raising ADX_STRONG_TREND_THRESHOLD in modes/aggressive.py"
            )
        if NOTIONAL_PER_TRADE is not None and NOTIONAL_PER_TRADE > 1000:
            problems.append(
                f"aggressive NOTIONAL_PER_TRADE=${NOTIONAL_PER_TRADE} is very large; "
                "monitor drawdowns closely"
            )

    # New entry-filter sanity checks (mode-aware signal strictness)
    if RSI_ENTRY_THRESHOLD is not None and not (20 <= RSI_ENTRY_THRESHOLD <= 80):
        problems.append(f"RSI_ENTRY_THRESHOLD={RSI_ENTRY_THRESHOLD} is outside reasonable range [20, 80]")
    if REQUIRE_BULLISH_TRIGGER is not None and not isinstance(REQUIRE_BULLISH_TRIGGER, bool):
        problems.append("REQUIRE_BULLISH_TRIGGER must be a boolean")
    if REQUIRE_NEAR_UPPER_BAND is not None and not isinstance(REQUIRE_NEAR_UPPER_BAND, bool):
        problems.append("REQUIRE_NEAR_UPPER_BAND must be a boolean")
    if BB_SQUEEZE_MAX_WIDTH_PCT is not None and not (0.01 <= BB_SQUEEZE_MAX_WIDTH_PCT <= 0.20):
        problems.append(f"BB_SQUEEZE_MAX_WIDTH_PCT={BB_SQUEEZE_MAX_WIDTH_PCT} is outside reasonable range [0.01, 0.20]")

    # Daily compensating filter validation
    if REQUIRE_ADX_RISING is not None and not isinstance(REQUIRE_ADX_RISING, bool):
        problems.append("REQUIRE_ADX_RISING must be a boolean")
    if REQUIRE_VOLUME_CONFIRMATION is not None and not isinstance(REQUIRE_VOLUME_CONFIRMATION, bool):
        problems.append("REQUIRE_VOLUME_CONFIRMATION must be a boolean")
    if LONG_TERM_SMA_PERIOD is not None and not (0 <= LONG_TERM_SMA_PERIOD <= 500):
        problems.append(f"LONG_TERM_SMA_PERIOD={LONG_TERM_SMA_PERIOD} is outside reasonable range [0, 500]")

    if problems:
        msg = "; ".join(problems)
        _log.error("Mode safety problems for TRADING_MODE=%s: %s", TRADING_MODE, msg)
        # Hard-fail only on truly catastrophic (protection disabled or invalid caps)
        if any("disabled" in p.lower() or "negative" in p.lower() for p in problems):
            raise RuntimeError(f"Unsafe mode parameters detected: {msg}")
        # Otherwise just warn; operator can decide to proceed
        _log.warning("Proceeding with risky parameters. Monitor performance closely.")


_validate_mode_params()
