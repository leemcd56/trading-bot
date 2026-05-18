# Moderate mode — balanced risk/reward; make money without courting financial ruin.
#
# Philosophy:
#   - Accept moderate trends (ADX ≥ 18) to catch more opportunities without chasing noise.
#   - Standard daily/weekly trade caps keep activity meaningful but not frenetic.
#   - 5% stop-loss / 5% trail activation is a tried-and-true baseline for daily candles.
#   - Per-trade sizing at 1% equity keeps drawdowns manageable across a losing streak.

PARAMS = {
    # Trade frequency caps
    "MAX_DAILY_TRADES": 3,        # up to three new positions per day
    "MAX_WEEKLY_TRADES": 8,       # up to eight new positions per rolling 7 days

    # Exposure limits
    "MAX_OPEN_POSITIONS": 4,      # hold up to four symbols simultaneously

    # Stop-loss / trailing stop
    "STOP_LOSS_PCT": 0.05,        # exit if position drops 5% from entry
    "TRAIL_ACTIVATION_PCT": 0.05, # trailing stop arms once price is 5% above entry
    "TRAIL_PCT": 0.04,            # once armed, sell if price falls 4% from running high

    # Entry gate strictness
    "ADX_STRONG_TREND_THRESHOLD": 18,   # moderate trend threshold
    "NEAR_UPPER_BAND_TOLERANCE": 0.025, # within 2.5% of BB upper band counts as extended
    "SIMILAR_TO_YESTERDAY_PCT": 0.01,   # skip if day's move vs prior close < 1%

    # Position sizing
    "RISK_PCT_PER_TRADE": 0.01,         # risk 1% of equity per trade
    "MAX_POSITION_PCT_EQUITY": 0.10,    # cap any single position at 10% of equity
    "MIN_SHARES": 1,
    "MAX_SHARES": 100,
    "NOTIONAL_PER_TRADE": 75,           # $75 per fractional buy
}
