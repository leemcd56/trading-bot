# Aggressive mode — bleeding-edge day trading; maximize activity and position size.
#
# Philosophy:
#   - Lower ADX bar accepts weaker/emerging trends to enter early.
#   - Higher trade caps let the bot act on every signal the market offers.
#   - Tight stop-loss (3%) cuts losses fast; tight trailing stop locks in gains quickly.
#   - Trail activates early (2% above entry) so small gains are never given back.
#   - Larger notional and higher equity allocation per trade magnify both wins and losses.
#   - Loosest entry filters: lowest RSI floor, no mandatory fresh trigger, only extreme
#     squeezes block entries, no near-upper-band requirement.
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
    "MAX_PORTFOLIO_RISK_PCT": 0.12,  # at most 12% of equity at risk across all open positions at once
    "MAX_POSITION_CORRELATION": 0.9,  # barely restricts - aggressive wants to act on every signal
    "MAX_SLIPPAGE_PCT": 0.01,  # cap qty orders 1% from the decision-time price (marketable limit)
    "CIRCUIT_BREAKER_ENABLED": True,
    "CIRCUIT_BREAKER_DRAWDOWN_PCT": 0.08,  # block new entries after an 8% intraday equity drop

    # Stop-loss / trailing stop
    "STOP_LOSS_PCT": 0.03,        # exit if position drops 3% from entry (cut fast)
    "TRAIL_ACTIVATION_PCT": 0.02, # trailing stop arms once price is 2% above entry
    "TRAIL_PCT": 0.025,           # once armed, sell if price falls 2.5% from running high

    # Entry gate strictness (loosest — early entries on emerging momentum)
    "ADX_STRONG_TREND_THRESHOLD": 14,   # accept nascent/emerging trends
    "NEAR_UPPER_BAND_TOLERANCE": 0.03,  # within 3% of BB upper band counts as extended
    "SIMILAR_TO_YESTERDAY_PCT": 0.005,  # only skip truly flat days (< 0.5% move)
    "RSI_ENTRY_THRESHOLD": 45,          # RSI > 45 — willing to enter on weaker momentum
    "REQUIRE_BULLISH_TRIGGER": False,   # do not require a fresh crossover or SAR flip
    "BB_SQUEEZE_MAX_WIDTH_PCT": 0.03,   # only extremely tight bands (<3%) count as a squeeze to avoid
    "REQUIRE_NEAR_UPPER_BAND": False,   # enter on trend strength without needing upper-band extension

    # Daily-bar compensating filters — aggressive turns these off for activity.
    # ADX rising alone was rejecting most early trends on daily bars.
    "REQUIRE_ADX_RISING": False,
    "REQUIRE_VOLUME_CONFIRMATION": False, # volume filter turned off (accepts lower-volume early moves)
    "LONG_TERM_SMA_PERIOD": 0,          # disabled — aggressive does not require major trend alignment

    # Min hold before discretionary TA signal exits (stops still fire immediately)
    "MIN_HOLD_HOURS": 0,                # can flip same day when risk allows

    # Position sizing
    "RISK_PCT_PER_TRADE": 0.02,         # risk 2% of equity per trade
    "MAX_POSITION_PCT_EQUITY": 0.15,    # cap any single position at 15% of equity
    "MIN_SHARES": 1,
    "MAX_SHARES": 200,
    "NOTIONAL_PER_TRADE": 150,          # $150 per fractional buy

    # Mean-reversion counter-strategy (regime switching) — OFF by default.
    "MEAN_REVERSION_ENABLED": False,
    "MEAN_REVERSION_ADX_CEILING": 10,          # below the already-low ADX=14 trend threshold
    "MEAN_REVERSION_RSI_OVERSOLD": 32,
    "MEAN_REVERSION_RSI_OVERBOUGHT": 68,
    "MEAN_REVERSION_STOP_LOSS_PCT": 0.025,     # tighter than the already-tight 3% trend stop
    "MEAN_REVERSION_RISK_PCT_PER_TRADE": 0.01,   # half the trend strategy's 2%
}
