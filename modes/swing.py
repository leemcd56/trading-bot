# Swing mode — ride multi-day trends; let winners run before exiting.
#
# Philosophy:
#   - Enter on moderate-to-strong trends (ADX ≥ 20) — slightly more selective than moderate.
#   - Fewer trades per week; each position is expected to be held for days to weeks.
#   - Wide stop-loss (8%) and trailing stop (activates at +15%, retreats 8%) prevent being
#     shaken out by routine daily noise while still protecting against real reversals.
#   - Larger notional per trade since fewer, higher-conviction positions are taken.

PARAMS = {
    # Trade frequency caps
    "MAX_DAILY_TRADES": 2,        # at most two new positions opened per day
    "MAX_WEEKLY_TRADES": 5,       # at most five new positions per rolling 7 days

    # Exposure limits
    "MAX_OPEN_POSITIONS": 3,      # hold up to three symbols simultaneously

    # Stop-loss / trailing stop
    "STOP_LOSS_PCT": 0.08,        # exit if position drops 8% from entry
    "TRAIL_ACTIVATION_PCT": 0.15, # trailing stop arms only after a 15% gain (let it run)
    "TRAIL_PCT": 0.08,            # once armed, sell if price falls 8% from running high

    # Entry gate strictness
    "ADX_STRONG_TREND_THRESHOLD": 20,   # slightly above moderate — want a clear trend
    "NEAR_UPPER_BAND_TOLERANCE": 0.02,  # within 2% of BB upper band counts as extended
    "SIMILAR_TO_YESTERDAY_PCT": 0.01,   # skip if day's move vs prior close < 1%

    # Position sizing
    "RISK_PCT_PER_TRADE": 0.015,        # risk 1.5% of equity per trade
    "MAX_POSITION_PCT_EQUITY": 0.12,    # cap any single position at 12% of equity
    "MIN_SHARES": 1,
    "MAX_SHARES": 150,
    "NOTIONAL_PER_TRADE": 100,          # $100 per fractional buy
}
