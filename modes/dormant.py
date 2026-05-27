# Dormant mode — analysis and alerts run normally; no orders are submitted.
#
# Philosophy:
#   - MAX_DAILY_TRADES=0 means the daily-cap check always blocks execution before
#     any order reaches Alpaca.  The full TA loop still runs: data is fetched,
#     indicators are computed, signals are logged, and Discord alerts fire — you
#     just won't see any BUY or SELL orders.
#   - Use this when you want to observe signal quality, evaluate a new watchlist,
#     or simply pause trading while remaining connected to the market.
#   - Switch back to any active mode and restart to resume trading.
#
# All non-frequency parameters are copied from moderate so that if you accidentally
# leave the bot in dormant mode and flip it back, the risk settings are sane.

PARAMS = {
    # Trade frequency caps — zero means the daily cap is never satisfied, blocking all orders
    "MAX_DAILY_TRADES": 0,
    "MAX_WEEKLY_TRADES": 0,

    # The remaining params are inherited from moderate; they are unused while dormant
    # but keep config valid and make the transition back to an active mode seamless.
    "MAX_OPEN_POSITIONS": 4,
    "STOP_LOSS_PCT": 0.05,
    "TRAIL_ACTIVATION_PCT": 0.05,
    "TRAIL_PCT": 0.04,
    "ADX_STRONG_TREND_THRESHOLD": 18,
    "NEAR_UPPER_BAND_TOLERANCE": 0.025,
    "SIMILAR_TO_YESTERDAY_PCT": 0.01,
    "RSI_ENTRY_THRESHOLD": 50,
    "REQUIRE_BULLISH_TRIGGER": False,
    "BB_SQUEEZE_MAX_WIDTH_PCT": 0.04,
    "REQUIRE_NEAR_UPPER_BAND": False,
    # Daily-bar compensating filters (inherited from moderate for seamlessness when switching modes)
    "REQUIRE_ADX_RISING": True,
    "REQUIRE_VOLUME_CONFIRMATION": True,
    "LONG_TERM_SMA_PERIOD": 100,
    "RISK_PCT_PER_TRADE": 0.01,
    "MAX_POSITION_PCT_EQUITY": 0.10,
    "MIN_SHARES": 1,
    "MAX_SHARES": 100,
    "NOTIONAL_PER_TRADE": None,
}
