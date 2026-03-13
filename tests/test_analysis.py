"""
Tests for analyze_trends using spoofed OHLC data.
Verify the bot's decisions: e.g. 3-day downtrend -> no buy, stay put or sell signals.
"""
import duckdb
import pytest
from analysis import analyze_trends
from tests.helpers import duckdb_with_spoofed_data, make_ohlc, load_spoofed_into_duckdb, NUM_BARS


def test_downtrend_three_days_no_buy_signal():
    """
    Spoof a downward trend over ~300 bars (e.g. 3 days of 1-min).
    Bot should NOT generate a buy: no trending_up_a_lot, and we should see
    downtrend characteristics (uptrend False, possibly dive_bombing or bearish signals).
    """
    con = duckdb_with_spoofed_data("TEST", start_price=100.0, end_price=85.0, num_bars=NUM_BARS)
    result = analyze_trends("TEST", connection=con)
    con.close()
    assert result is not None
    # Should not trigger buy: either no strong uptrend or missing buy conditions
    assert result["trending_up_a_lot"] is False or result["uptrend"] is False
    # Downtrend: +DI < -DI
    assert result["uptrend"] is False
    # Should not incorrectly signal buy
    assert not (
        result.get("trending_up_a_lot")
        and result.get("near_upper_band")
        and result.get("sar_below_price")
        and (result.get("bullish_crossover") or result.get("sar_flipped_to_bull"))
        and not result.get("similar_to_yesterday", True)
        and not result.get("bb_squeeze", True)
    )


def test_downtrend_dive_bombing_or_sell_signals():
    """
    Strong downtrend with RSI low and sharp drop -> dive_bombing or other sell signals
    may be True (depending on exact spoofed curve). At least we have downtrend.
    """
    con = duckdb_with_spoofed_data("TEST", start_price=100.0, end_price=75.0, num_bars=NUM_BARS, noise=0.1)
    result = analyze_trends("TEST", connection=con)
    con.close()
    assert result is not None
    assert result["uptrend"] is False
    # With a big drop, we may get dive_bombing or bearish_crossover / near_lower_band
    assert "dive_bombing" in result
    assert "bearish_crossover" in result
    assert "near_lower_band" in result


def test_insufficient_data_returns_none():
    """Fewer than 50 bars -> analyze_trends returns None."""
    df = make_ohlc("TEST", 100.0, 99.0, num_bars=40)
    con = duckdb.connect(":memory:")
    load_spoofed_into_duckdb(df, con)
    result = analyze_trends("TEST", connection=con)
    con.close()
    assert result is None


def test_analysis_returns_all_keys_required_by_trading():
    """Ensure the return dict has every key trading.execute_trade reads."""
    con = duckdb_with_spoofed_data("TEST", start_price=100.0, end_price=105.0, num_bars=NUM_BARS)
    result = analyze_trends("TEST", connection=con)
    con.close()
    assert result is not None
    required = [
        "strong_trend", "uptrend", "sar_below_price", "sar_above_price",
        "near_upper_band", "near_lower_band", "bb_squeeze",
        "bullish_crossover", "bearish_crossover", "sar_flipped_to_bull", "sar_flipped_to_bear",
        "trending_up_a_lot", "similar_to_yesterday", "dive_bombing", "current_price",
    ]
    for k in required:
        assert k in result, f"Missing key: {k}"
