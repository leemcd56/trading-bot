# Conservative mode — build a nest egg with infrequent, high-conviction trades.
#
# Philosophy:
#   - Only enter when the trend is unambiguously strong (ADX ≥ 25, classic threshold).
#   - Fewer trades per week; smaller per-trade size.
#   - Wider initial stop gives positions room to breathe without being shaken out.
#   - Trailing stop activates only after a meaningful gain, then locks in profits tightly.
#   - Tight open-position cap keeps concentration risk low.
#   - Strict entry filters: higher RSI floor, mandatory fresh trigger (crossover or SAR flip),
#     and requires price to be near the upper Bollinger Band for conviction.

PARAMS = {
    # Trade frequency caps
    "MAX_DAILY_TRADES": 1,        # at most one new position opened per day
    "MAX_WEEKLY_TRADES": 3,       # at most three new positions per rolling 7 days

    # Exposure limits
    "MAX_OPEN_POSITIONS": 2,      # never hold more than two symbols simultaneously
    "MAX_PORTFOLIO_RISK_PCT": 0.01,  # at most 1% of equity at risk across all open positions at once
    "MAX_POSITION_CORRELATION": 0.6,  # strict: refuse a new symbol too correlated with one already held
    "MAX_SLIPPAGE_PCT": 0.003,  # cap qty orders 0.3% from the decision-time price (marketable limit)

    # Stop-loss / trailing stop
    "STOP_LOSS_PCT": 0.07,        # exit if position drops 7% from entry
    "TRAIL_ACTIVATION_PCT": 0.10, # trailing stop arms once price is 10% above entry
    "TRAIL_PCT": 0.05,            # once armed, sell if price falls 5% from running high

    # Entry gate strictness (high-conviction only)
    "ADX_STRONG_TREND_THRESHOLD": 25,   # classic "strong trend" ADX cutoff
    "NEAR_UPPER_BAND_TOLERANCE": 0.015, # within 1.5% of BB upper band counts as extended
    "SIMILAR_TO_YESTERDAY_PCT": 0.02,   # skip if day's move vs prior close < 2%
    "RSI_ENTRY_THRESHOLD": 55,          # RSI > 55 for strong bullish momentum
    "REQUIRE_BULLISH_TRIGGER": True,    # require a fresh crossover or SAR flip for entry
    "BB_SQUEEZE_MAX_WIDTH_PCT": 0.05,   # higher threshold = stricter squeeze avoidance (blocks more compressed-vol situations)
    "REQUIRE_NEAR_UPPER_BAND": True,    # require price to be extended near upper band for high-conviction entries

    # Daily-bar compensating filters (effective on daily candles; conservative keeps them strict)
    "REQUIRE_ADX_RISING": True,         # ADX today > ADX 5 bars ago (trend is strengthening)
    "REQUIRE_VOLUME_CONFIRMATION": True, # current volume > 20-day SMA volume
    "LONG_TERM_SMA_PERIOD": 200,        # price must be above 200-day SMA (major trend bias)

    # Min hold before discretionary TA signal exits (stops still fire immediately)
    "MIN_HOLD_HOURS": 24,               # overnight minimum; avoid whipsaw exits

    # Position sizing
    "RISK_PCT_PER_TRADE": 0.005,        # risk 0.5% of equity per trade
    "MAX_POSITION_PCT_EQUITY": 0.08,    # cap any single position at 8% of equity
    "MIN_SHARES": 1,
    "MAX_SHARES": 50,
    "NOTIONAL_PER_TRADE": 50,           # $50 per fractional buy

    # Mean-reversion counter-strategy (regime switching) — OFF by default.
    # Conservative's whole premise is "sit out anything that isn't a clear
    # trend," so this is the strategy's intentionally strictest, smallest
    # configuration for anyone who deliberately opts in.
    "MEAN_REVERSION_ENABLED": False,
    "MEAN_REVERSION_ADX_CEILING": 12,          # well below the ADX=25 trend threshold; leaves a wide dead zone
    "MEAN_REVERSION_RSI_OVERSOLD": 25,         # stricter than the other modes' 30
    "MEAN_REVERSION_RSI_OVERBOUGHT": 75,
    "MEAN_REVERSION_STOP_LOSS_PCT": 0.03,      # tighter than the 7% trend stop
    "MEAN_REVERSION_RISK_PCT_PER_TRADE": 0.0025,  # half the trend strategy's already-small 0.5%
}
