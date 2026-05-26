# Aggressive mode — bleeding-edge day trading; maximize activity and position size.
#
# Philosophy:
#   - Lower ADX bar accepts weaker/emerging trends to enter early.
#   - Higher trade caps let the bot act on every signal the market offers.
#   - Tight stop-loss (3%) cuts losses fast; tight trailing stop locks in gains quickly.
#   - Trail activates early (2% above entry) so small gains are never given back.
#   - Larger notional and higher equity allocation per trade magnify both wins and losses.
#
# WARNING: aggressive mode carries significantly higher drawdown risk.  Suitable only
# for capital you can afford to lose and accounts not subject to PDT restrictions.
#
# SAFETY NOTE: All keys below are required. If any are missing at runtime,
# config.py supplies conservative fallbacks (high ADX, wide stops, NOTIONAL=None,
# low caps) + a startup warning. This prevents a broken aggressive mode file
# from becoming even more dangerous. Keep this file complete.

PARAMS = {
    # Trade frequency caps
    "MAX_DAILY_TRADES": 6,        # up to six new positions per day
    "MAX_WEEKLY_TRADES": 15,      # up to fifteen new positions per rolling 7 days

    # Exposure limits
    "MAX_OPEN_POSITIONS": 6,      # hold up to six symbols simultaneously

    # Stop-loss / trailing stop
    "STOP_LOSS_PCT": 0.03,        # exit if position drops 3% from entry (cut fast)
    "TRAIL_ACTIVATION_PCT": 0.02, # trailing stop arms once price is 2% above entry
    "TRAIL_PCT": 0.025,           # once armed, sell if price falls 2.5% from running high

    # Entry gate strictness
    "ADX_STRONG_TREND_THRESHOLD": 14,   # accept nascent/emerging trends
    "NEAR_UPPER_BAND_TOLERANCE": 0.03,  # within 3% of BB upper band counts as extended
    "SIMILAR_TO_YESTERDAY_PCT": 0.005,  # only skip truly flat days (< 0.5% move)

    # Position sizing
    "RISK_PCT_PER_TRADE": 0.02,         # risk 2% of equity per trade
    "MAX_POSITION_PCT_EQUITY": 0.15,    # cap any single position at 15% of equity
    "MIN_SHARES": 1,
    "MAX_SHARES": 200,
    "NOTIONAL_PER_TRADE": 150,          # $150 per fractional buy
}
