# Moderate mode — balanced risk/reward; make money without courting financial ruin.
#
# Philosophy:
#   - Accept moderate trends (ADX ≥ 18) to catch more opportunities without chasing noise.
#   - Standard daily/weekly trade caps keep activity meaningful but not frenetic.
#   - 5% stop-loss / 5% trail activation is a tried-and-true baseline for daily candles.
#   - Per-trade sizing via 1% risk + ATR (NOTIONAL=None) keeps drawdowns manageable.
#   - Relaxed entry filters vs conservative: lower RSI floor, no mandatory fresh trigger,
#     no requirement to be near the upper Bollinger Band. Still avoids true noise via
#     squeeze filter, avoid_long heuristics, and similarity check.
#
# SAFETY NOTE: All keys below are required. If any are missing at runtime,
# config.py supplies conservative fallbacks (high ADX, wide stops, NOTIONAL=None,
# low caps) + a startup warning. This prevents a broken mode file from
# silently disabling protection or using dangerous values (e.g. ADX=0 or
# stops disabled). Keep this file complete.

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

    # Entry gate strictness (moderate = balanced, not high-conviction only)
    "ADX_STRONG_TREND_THRESHOLD": 18,   # moderate trend threshold
    "NEAR_UPPER_BAND_TOLERANCE": 0.025, # within 2.5% of BB upper band counts as extended
    "SIMILAR_TO_YESTERDAY_PCT": 0.01,   # skip if day's move vs prior close < 1%
    "RSI_ENTRY_THRESHOLD": 50,          # RSI > 50 for bullish momentum (moderate floor)
    "REQUIRE_BULLISH_TRIGGER": False,   # allow entry on established trend strength (no fresh crossover/SAR flip required)
    "BB_SQUEEZE_MAX_WIDTH_PCT": 0.04,   # treat bands narrower than 4% as a squeeze to avoid
    "REQUIRE_NEAR_UPPER_BAND": False,   # do not require price to be extended near upper band

    # Daily-bar compensating filters (effective on daily candles)
    "REQUIRE_ADX_RISING": True,         # ADX today > ADX 5 bars ago (trend strengthening)
    "REQUIRE_VOLUME_CONFIRMATION": True, # current volume > 20-day SMA volume
    "LONG_TERM_SMA_PERIOD": 100,        # price must be above 100-day SMA (intermediate trend bias)

    # Position sizing (risk-based is preferred for moderate)
    "RISK_PCT_PER_TRADE": 0.01,         # risk 1% of equity per trade
    "MAX_POSITION_PCT_EQUITY": 0.10,    # cap any single position at 10% of equity
    "MIN_SHARES": 1,
    "MAX_SHARES": 100,
    "NOTIONAL_PER_TRADE": None,         # None = use RISK_PCT_PER_TRADE + ATR for proper risk sizing
}
