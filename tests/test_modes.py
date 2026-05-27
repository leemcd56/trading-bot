"""
Tests for the trading-mode system.

Three layers:
  1. Mode files — every mode has all required keys; value invariants hold.
  2. Config loading — TRADING_MODE env var selects the right mode; invalid names raise.
  3. Trading behavior — mode-specific stop-loss, trail, and trade-cap params are
     respected by execute_trade when patched onto the trading module.
"""
import importlib
import os
from unittest.mock import MagicMock, patch

import pytest

# ─── shared fixtures ──────────────────────────────────────────────────────────

# Set Alpaca env vars before importing trading so TradingClient(...) does not raise.
os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

import trading
from alpaca.trading.enums import OrderSide

REQUIRED_KEYS = [
    "MAX_DAILY_TRADES",
    "MAX_WEEKLY_TRADES",
    "MAX_OPEN_POSITIONS",
    "STOP_LOSS_PCT",
    "TRAIL_ACTIVATION_PCT",
    "TRAIL_PCT",
    "ADX_STRONG_TREND_THRESHOLD",
    "NEAR_UPPER_BAND_TOLERANCE",
    "SIMILAR_TO_YESTERDAY_PCT",
    # Mode-aware entry filters (from earlier work)
    "RSI_ENTRY_THRESHOLD",
    "REQUIRE_BULLISH_TRIGGER",
    "BB_SQUEEZE_MAX_WIDTH_PCT",
    "REQUIRE_NEAR_UPPER_BAND",
    # New daily-bar compensating filters (ADX rising, volume, long-term MA)
    "REQUIRE_ADX_RISING",
    "REQUIRE_VOLUME_CONFIRMATION",
    "LONG_TERM_SMA_PERIOD",
    "RISK_PCT_PER_TRADE",
    "MAX_POSITION_PCT_EQUITY",
    "MIN_SHARES",
    "MAX_SHARES",
    "NOTIONAL_PER_TRADE",
]

ALL_MODES = ["conservative", "moderate", "aggressive", "swing", "dormant"]


def _load_mode(name: str) -> dict:
    return importlib.import_module(f"modes.{name}").PARAMS


def _patch_trade_limits():
    """Patch DB-touching helpers so tests never hit the real database."""
    return patch.multiple(
        trading,
        _count_daily=lambda: 0,
        _count_weekly=lambda: 0,
        _record_trade=lambda symbol, side, qty=0: None,
        _record_trade_history=lambda *a, **kw: None,
        _should_block_sell_pdt=lambda symbol: False,
        _count_day_trades_in_last_5_days=lambda: 0,
        _get_trail_running_high=lambda symbol: None,
        _set_trail_running_high=lambda symbol, running_high: None,
        _clear_trail_state=lambda symbol: None,
    )


def _full_buy_analysis(current_price: float = 100.0) -> dict:
    """Analysis dict that satisfies all BUY conditions (including the new daily compensating filters)."""
    return {
        "strong_trend": True,
        "uptrend": True,
        "trending_up_a_lot": True,
        "sar_below_price": True,
        "bullish_crossover": True,
        "sar_flipped_to_bull": False,
        "bullish_crossover_recent": False,
        "sar_flipped_to_bull_recent": False,
        "similar_to_yesterday": False,
        "bb_squeeze": False,
        "avoid_long": False,
        "near_lower_band": False,
        "sar_above_price": False,
        "sar_flipped_to_bear": False,
        "dive_bombing": False,
        "bearish_crossover": False,
        "current_price": current_price,
        "atr_14": 1.0,
        # New daily compensating filter flags — set True so existing behavior tests (stops, trails, notional, etc.)
        # continue to isolate the logic they care about instead of being blocked by the new gates.
        "adx_rising": True,
        "volume_confirmed": True,
        "above_long_term_ma": True,
        "near_upper_band": True,  # needed for the conservative notional test that expects the old band gate
    }


# ─── 1. Mode file completeness & invariants ───────────────────────────────────


@pytest.mark.parametrize("mode_name", ALL_MODES)
def test_mode_has_all_required_keys(mode_name):
    """Every mode must export every key that config.py reads."""
    params = _load_mode(mode_name)
    missing = [k for k in REQUIRED_KEYS if k not in params]
    assert missing == [], f"{mode_name} is missing keys: {missing}"


@pytest.mark.parametrize("mode_name", ALL_MODES)
def test_mode_values_are_positive(mode_name):
    """All numeric params in every mode must be non-negative (or None for NOTIONAL_PER_TRADE, 0 for LONG_TERM_SMA_PERIOD)."""
    params = _load_mode(mode_name)
    for key in REQUIRED_KEYS:
        val = params[key]
        if key == "NOTIONAL_PER_TRADE":
            # None is valid (means use risk/ATR sizing instead of fixed notional)
            if val is None:
                continue
        if key == "LONG_TERM_SMA_PERIOD":
            # 0 is valid (disabled for aggressive)
            if val == 0:
                continue
        assert val is not None and val >= 0, f"{mode_name}.{key} must be >= 0 (or allowed None/0), got {val}"


def test_aggressive_trades_more_than_moderate():
    ag = _load_mode("aggressive")
    mo = _load_mode("moderate")
    assert ag["MAX_DAILY_TRADES"] > mo["MAX_DAILY_TRADES"]
    assert ag["MAX_WEEKLY_TRADES"] > mo["MAX_WEEKLY_TRADES"]
    assert ag["MAX_OPEN_POSITIONS"] > mo["MAX_OPEN_POSITIONS"]


def test_conservative_trades_less_than_moderate():
    co = _load_mode("conservative")
    mo = _load_mode("moderate")
    assert co["MAX_DAILY_TRADES"] < mo["MAX_DAILY_TRADES"]
    assert co["MAX_WEEKLY_TRADES"] < mo["MAX_WEEKLY_TRADES"]
    assert co["MAX_OPEN_POSITIONS"] < mo["MAX_OPEN_POSITIONS"]


def test_aggressive_stop_loss_tighter_than_conservative():
    """Aggressive cuts losses faster; conservative gives positions more room."""
    ag = _load_mode("aggressive")
    co = _load_mode("conservative")
    assert ag["STOP_LOSS_PCT"] < co["STOP_LOSS_PCT"]


def test_swing_trail_wider_than_all_other_modes():
    """Swing mode holds winners longest — both trail activation and distance must be widest."""
    sw = _load_mode("swing")
    for name in ["conservative", "moderate", "aggressive"]:
        other = _load_mode(name)
        assert sw["TRAIL_ACTIVATION_PCT"] > other["TRAIL_ACTIVATION_PCT"], (
            f"swing TRAIL_ACTIVATION_PCT should exceed {name}"
        )
        assert sw["TRAIL_PCT"] > other["TRAIL_PCT"], (
            f"swing TRAIL_PCT should exceed {name}"
        )


def test_aggressive_trail_activates_soonest():
    """Aggressive locks in gains earlier than any other active mode."""
    ag = _load_mode("aggressive")
    for name in ["conservative", "moderate", "swing"]:
        other = _load_mode(name)
        assert ag["TRAIL_ACTIVATION_PCT"] < other["TRAIL_ACTIVATION_PCT"], (
            f"aggressive TRAIL_ACTIVATION_PCT should be smaller than {name}"
        )


def test_dormant_blocks_all_trades():
    """Dormant mode must set both trade caps to zero."""
    do = _load_mode("dormant")
    assert do["MAX_DAILY_TRADES"] == 0
    assert do["MAX_WEEKLY_TRADES"] == 0


def test_aggressive_notional_larger_than_conservative():
    ag = _load_mode("aggressive")
    co = _load_mode("conservative")
    assert ag["NOTIONAL_PER_TRADE"] > co["NOTIONAL_PER_TRADE"]


def test_aggressive_adx_threshold_lowest():
    """Aggressive accepts weaker trends; its ADX threshold should be the lowest."""
    ag = _load_mode("aggressive")
    for name in ["conservative", "moderate", "swing"]:
        other = _load_mode(name)
        assert ag["ADX_STRONG_TREND_THRESHOLD"] < other["ADX_STRONG_TREND_THRESHOLD"], (
            f"aggressive ADX threshold should be lower than {name}"
        )


def test_conservative_adx_threshold_highest():
    """Conservative demands the strongest trend before entering."""
    co = _load_mode("conservative")
    for name in ["moderate", "aggressive", "swing"]:
        other = _load_mode(name)
        assert co["ADX_STRONG_TREND_THRESHOLD"] > other["ADX_STRONG_TREND_THRESHOLD"], (
            f"conservative ADX threshold should be higher than {name}"
        )


# New entry-filter differentiation tests (the core of making moderate a "proper" moderate mode)


def test_moderate_has_lower_rsi_threshold_than_conservative():
    """Moderate accepts weaker momentum; its RSI floor should be below conservative."""
    mo = _load_mode("moderate")
    co = _load_mode("conservative")
    assert mo["RSI_ENTRY_THRESHOLD"] < co["RSI_ENTRY_THRESHOLD"]


def test_aggressive_has_lowest_rsi_threshold():
    """Aggressive is willing to enter on the weakest momentum signals."""
    ag = _load_mode("aggressive")
    for name in ["conservative", "moderate", "swing"]:
        other = _load_mode(name)
        assert ag["RSI_ENTRY_THRESHOLD"] < other["RSI_ENTRY_THRESHOLD"], (
            f"aggressive RSI threshold should be lower than {name}"
        )


def test_moderate_does_not_require_bullish_trigger():
    """Moderate can enter on established trend strength without a fresh discrete signal."""
    mo = _load_mode("moderate")
    assert mo["REQUIRE_BULLISH_TRIGGER"] is False


def test_conservative_requires_bullish_trigger():
    """Conservative demands a fresh trigger for high-conviction entries."""
    co = _load_mode("conservative")
    assert co["REQUIRE_BULLISH_TRIGGER"] is True


def test_moderate_does_not_require_near_upper_band():
    """Moderate does not force entries to be extended near the upper band."""
    mo = _load_mode("moderate")
    assert mo["REQUIRE_NEAR_UPPER_BAND"] is False


def test_conservative_requires_near_upper_band():
    """Conservative wants high-conviction band-riding entries."""
    co = _load_mode("conservative")
    assert co["REQUIRE_NEAR_UPPER_BAND"] is True


def test_aggressive_has_strictest_squeeze_avoidance():
    """Aggressive only treats extremely narrow bands as a 'squeeze' to avoid."""
    ag = _load_mode("aggressive")
    for name in ["conservative", "moderate", "swing"]:
        other = _load_mode(name)
        assert ag["BB_SQUEEZE_MAX_WIDTH_PCT"] < other["BB_SQUEEZE_MAX_WIDTH_PCT"], (
            f"aggressive squeeze threshold should be lower than {name}"
        )


# New daily compensating filter tests (the main request for fixing aggressive on daily data)


def test_conservative_requires_adx_rising_and_volume():
    """Conservative uses the strongest daily filters."""
    co = _load_mode("conservative")
    assert co["REQUIRE_ADX_RISING"] is True
    assert co["REQUIRE_VOLUME_CONFIRMATION"] is True
    assert co["LONG_TERM_SMA_PERIOD"] == 200


def test_moderate_requires_daily_filters_but_shorter_ma():
    """Moderate keeps the daily guardrails but uses a shorter long-term MA."""
    mo = _load_mode("moderate")
    assert mo["REQUIRE_ADX_RISING"] is True
    assert mo["REQUIRE_VOLUME_CONFIRMATION"] is True
    assert mo["LONG_TERM_SMA_PERIOD"] == 100


def test_aggressive_relaxes_most_daily_filters_but_keeps_adx_rising():
    """Aggressive turns off volume and long-term MA (to stay aggressive) but still requires ADX rising
    — this single filter is one of the highest-ROI improvements possible on daily bars."""
    ag = _load_mode("aggressive")
    assert ag["REQUIRE_ADX_RISING"] is True
    assert ag["REQUIRE_VOLUME_CONFIRMATION"] is False
    assert ag["LONG_TERM_SMA_PERIOD"] == 0


def test_swing_uses_strict_daily_filters():
    """Swing wants clean, high-quality daily setups for multi-day holds."""
    sw = _load_mode("swing")
    assert sw["REQUIRE_ADX_RISING"] is True
    assert sw["REQUIRE_VOLUME_CONFIRMATION"] is True
    assert sw["LONG_TERM_SMA_PERIOD"] == 200


# ─── 2. Config loading ────────────────────────────────────────────────────────


def _reload_config(mode: str) -> object:
    """Reload config with TRADING_MODE set to `mode`."""
    os.environ["TRADING_MODE"] = mode
    os.environ.setdefault("MOTHERDUCK_TOKEN", "test-token")
    import config
    return importlib.reload(config)


@pytest.mark.parametrize("mode_name", ALL_MODES)
def test_config_loads_correct_mode(mode_name):
    """config.py must expose the correct TRADING_MODE string after reload."""
    cfg = _reload_config(mode_name)
    assert cfg.TRADING_MODE == mode_name


@pytest.mark.parametrize("mode_name", ALL_MODES)
def test_config_params_match_mode_file(mode_name):
    """config.py module-level constants must match the selected mode file."""
    expected = _load_mode(mode_name)
    cfg = _reload_config(mode_name)
    assert cfg.MAX_OPEN_POSITIONS == expected["MAX_OPEN_POSITIONS"]
    assert cfg.STOP_LOSS_PCT == expected["STOP_LOSS_PCT"]
    assert cfg.TRAIL_ACTIVATION_PCT == expected["TRAIL_ACTIVATION_PCT"]
    assert cfg.TRAIL_PCT == expected["TRAIL_PCT"]
    assert cfg.ADX_STRONG_TREND_THRESHOLD == expected["ADX_STRONG_TREND_THRESHOLD"]
    assert cfg.NOTIONAL_PER_TRADE == expected["NOTIONAL_PER_TRADE"]


def test_config_rejects_invalid_mode():
    """An unrecognised TRADING_MODE must raise RuntimeError at import time."""
    os.environ["TRADING_MODE"] = "turbo_yolo"
    os.environ.setdefault("MOTHERDUCK_TOKEN", "test-token")
    import config
    with pytest.raises(RuntimeError, match="Invalid TRADING_MODE"):
        importlib.reload(config)


def test_config_env_override_max_daily_trades():
    """MAX_DAILY_TRADES env var must override the mode's default."""
    os.environ["TRADING_MODE"] = "moderate"
    os.environ["MAX_DAILY_TRADES"] = "99"
    os.environ.setdefault("MOTHERDUCK_TOKEN", "test-token")
    import config
    cfg = importlib.reload(config)
    assert cfg.MAX_DAILY_TRADES == 99
    del os.environ["MAX_DAILY_TRADES"]  # clean up so other tests aren't affected


def test_config_env_override_max_weekly_trades():
    """MAX_WEEKLY_TRADES env var must override the mode's default."""
    os.environ["TRADING_MODE"] = "moderate"
    os.environ["MAX_WEEKLY_TRADES"] = "42"
    os.environ.setdefault("MOTHERDUCK_TOKEN", "test-token")
    import config
    cfg = importlib.reload(config)
    assert cfg.MAX_WEEKLY_TRADES == 42
    del os.environ["MAX_WEEKLY_TRADES"]


# ─── 3. Trading behavior with mode-specific parameters ───────────────────────


def test_dormant_mode_blocks_buy_via_zero_daily_cap():
    """
    With MAX_DAILY_TRADES=0, the daily-cap check (_count_daily() >= 0) is always
    True and execute_trade must return without submitting any order.
    """
    with _patch_trade_limits(), \
         patch.object(trading, "MAX_DAILY_TRADES", 0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_all_positions.return_value = []
        trading.execute_trade("TEST", _full_buy_analysis())
        mock_client.submit_order.assert_not_called()


def test_aggressive_stop_loss_fires_at_3pct():
    """With aggressive STOP_LOSS_PCT=0.03, a 3.1% drop from entry must trigger SELL."""
    entry = 100.0
    current = entry * (1 - 0.031)   # 3.1% below entry → should trigger

    with _patch_trade_limits(), \
         patch.object(trading, "STOP_LOSS_PCT", 0.03), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price=str(entry)
        )
        trading.execute_trade("TEST", {"strong_trend": True, "current_price": current})
        mock_client.submit_order.assert_called_once()
        assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL


def test_aggressive_stop_loss_does_not_fire_at_2pct():
    """With aggressive STOP_LOSS_PCT=0.03, a 2% drop must NOT trigger stop-loss."""
    entry = 100.0
    current = entry * (1 - 0.02)   # only 2% below entry

    with _patch_trade_limits(), \
         patch.object(trading, "STOP_LOSS_PCT", 0.03), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price=str(entry)
        )
        analysis = {**_full_buy_analysis(current), "strong_trend": True}
        # No buy conditions active — just checking stop-loss doesn't fire
        analysis.update({
            "trending_up_a_lot": False, "sar_below_price": False,
            "near_lower_band": False, "sar_above_price": False,
            "sar_flipped_to_bear": False, "dive_bombing": False,
            "bearish_crossover": False, "bullish_crossover": False,
        })
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_conservative_stop_loss_fires_at_7pct():
    """With conservative STOP_LOSS_PCT=0.07, a 7.1% drop must trigger SELL."""
    entry = 100.0
    current = entry * (1 - 0.071)

    with _patch_trade_limits(), \
         patch.object(trading, "STOP_LOSS_PCT", 0.07), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price=str(entry)
        )
        trading.execute_trade("TEST", {"strong_trend": True, "current_price": current})
        mock_client.submit_order.assert_called_once()
        assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL


def test_conservative_stop_loss_does_not_fire_at_5pct():
    """With conservative STOP_LOSS_PCT=0.07, a 5% drop (moderate's trigger) must NOT fire."""
    entry = 100.0
    current = entry * (1 - 0.05)   # 5% — would trigger moderate but not conservative

    with _patch_trade_limits(), \
         patch.object(trading, "STOP_LOSS_PCT", 0.07), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price=str(entry)
        )
        analysis = {**_full_buy_analysis(current), "strong_trend": True}
        analysis.update({
            "trending_up_a_lot": False, "sar_below_price": False,
            "near_lower_band": False, "sar_above_price": False,
            "sar_flipped_to_bear": False, "dive_bombing": False,
            "bearish_crossover": False, "bullish_crossover": False,
        })
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_swing_trailing_stop_does_not_activate_at_10pct_gain():
    """
    Swing TRAIL_ACTIVATION_PCT=0.15: a 10% gain above entry must NOT activate the trail.
    The same price would activate moderate's trail (5%), but not swing's.
    """
    entry = 100.0
    current = entry * 1.10   # 10% gain — below swing's 15% activation threshold

    with _patch_trade_limits(), \
         patch.object(trading, "STOP_LOSS_PCT", 0.08), \
         patch.object(trading, "TRAIL_ACTIVATION_PCT", 0.15), \
         patch.object(trading, "TRAIL_PCT", 0.08), \
         patch.object(trading, "_get_trail_running_high", return_value=current), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price=str(entry)
        )
        analysis = {**_full_buy_analysis(current), "strong_trend": True}
        analysis.update({
            "trending_up_a_lot": False, "sar_below_price": False,
            "near_lower_band": False, "sar_above_price": False,
            "sar_flipped_to_bear": False, "dive_bombing": False,
            "bearish_crossover": False, "bullish_crossover": False,
        })
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_swing_trailing_stop_activates_and_fires_after_15pct_gain():
    """
    Swing: trail activates at +15%, retreats 8%.
    Entry=100, running_high=130 (30% above), current=118.
      trail_active:   118 >= 100*1.15=115  ✓
      trail trigger:  118 <= 130*0.92=119.6 ✓  → SELL
    """
    entry = 100.0
    running_high = 130.0
    current = 118.0

    with _patch_trade_limits(), \
         patch.object(trading, "STOP_LOSS_PCT", 0.08), \
         patch.object(trading, "TRAIL_ACTIVATION_PCT", 0.15), \
         patch.object(trading, "TRAIL_PCT", 0.08), \
         patch.object(trading, "_get_trail_running_high", return_value=running_high), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=3, avg_entry_price=str(entry)
        )
        trading.execute_trade("TEST", {"strong_trend": True, "current_price": current})
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.SELL
        assert float(order.qty) == 3


def test_aggressive_trailing_stop_activates_at_2pct_gain():
    """
    Aggressive: trail activates at +2%, retreats 2.5%.
    Entry=100, running_high=106, current=102.5.
      trail_active:   102.5 >= 100*1.02=102  ✓
      trail trigger:  102.5 <= 106*0.975=103.35 ✓  → SELL
    """
    entry = 100.0
    running_high = 106.0
    current = 102.5

    with _patch_trade_limits(), \
         patch.object(trading, "STOP_LOSS_PCT", 0.03), \
         patch.object(trading, "TRAIL_ACTIVATION_PCT", 0.02), \
         patch.object(trading, "TRAIL_PCT", 0.025), \
         patch.object(trading, "_get_trail_running_high", return_value=running_high), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price=str(entry)
        )
        trading.execute_trade("TEST", {"strong_trend": True, "current_price": current})
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.SELL


def test_aggressive_notional_used_in_buy_order():
    """With aggressive NOTIONAL_PER_TRADE=$150, fractional buy must use $150."""
    with _patch_trade_limits(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", 150), \
         patch.object(trading, "MAX_OPEN_POSITIONS", 6), \
         patch.object(trading, "_get_buying_power", return_value=500.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _full_buy_analysis(current_price=300.0)  # price > notional → fractional
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "notional", None) == 150.0


def test_conservative_notional_used_in_buy_order():
    """With conservative NOTIONAL_PER_TRADE=$50, fractional buy must use $50."""
    with _patch_trade_limits(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", 50), \
         patch.object(trading, "MAX_OPEN_POSITIONS", 2), \
         patch.object(trading, "_get_buying_power", return_value=500.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _full_buy_analysis(current_price=300.0)  # price > notional → fractional
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "notional", None) == 50.0


# ─── 4. Defensive fallback behavior (missing mode keys) ─────────────────────────
# These tests verify that config.py never lets critical risk variables
# (ADX, trail params, NOTIONAL, etc.) become dangerous when a mode file
# is missing a key. Safe conservative fallbacks + warnings protect the user.


def test_missing_mode_key_uses_safe_fallback_adx():
    """
    If a mode's PARAMS is missing ADX_STRONG_TREND_THRESHOLD, config must
    fall back to the safe conservative default (22) and still load without
    crashing or using a catastrophic value (e.g. 0 or None).
    """
    import sys

    os.environ["TRADING_MODE"] = "moderate"
    os.environ.setdefault("MOTHERDUCK_TOKEN", "test-token")

    mod = importlib.import_module("modes.moderate")
    original = mod.PARAMS.copy()

    try:
        broken = original.copy()
        broken.pop("ADX_STRONG_TREND_THRESHOLD", None)
        mod.PARAMS = broken

        # Force a clean load of config
        if "config" in sys.modules:
            del sys.modules["config"]
        import config as cfg
        cfg = importlib.reload(cfg)

        # Safe fallback for ADX is 22 (see config._SAFE_FALLBACKS)
        assert cfg.ADX_STRONG_TREND_THRESHOLD == 22
    finally:
        mod.PARAMS = original
        if "config" in sys.modules:
            del sys.modules["config"]


def test_missing_mode_key_uses_safe_fallback_notional_and_trail():
    """
    Missing NOTIONAL_PER_TRADE falls back to None (enables ATR/risk-based
    sizing in trading.py). Missing trail params also get safe defaults.
    """
    import sys

    os.environ["TRADING_MODE"] = "moderate"
    os.environ.setdefault("MOTHERDUCK_TOKEN", "test-token")

    mod = importlib.import_module("modes.moderate")
    original = mod.PARAMS.copy()

    try:
        broken = original.copy()
        broken.pop("NOTIONAL_PER_TRADE", None)
        broken.pop("TRAIL_ACTIVATION_PCT", None)
        broken.pop("TRAIL_PCT", None)
        mod.PARAMS = broken

        if "config" in sys.modules:
            del sys.modules["config"]
        import config as cfg
        cfg = importlib.reload(cfg)

        assert cfg.NOTIONAL_PER_TRADE is None
        # Safe trail fallbacks
        assert cfg.TRAIL_ACTIVATION_PCT == 0.10
        assert cfg.TRAIL_PCT == 0.06
    finally:
        mod.PARAMS = original
        if "config" in sys.modules:
            del sys.modules["config"]


def test_broken_stop_loss_raises_on_validation():
    """
    If a critical protection value (STOP_LOSS_PCT) is present but <= 0,
    the post-load validation in config.py must raise RuntimeError so the
    bot cannot start in a state with disabled stops.
    """
    import sys

    os.environ["TRADING_MODE"] = "moderate"
    os.environ.setdefault("MOTHERDUCK_TOKEN", "test-token")

    mod = importlib.import_module("modes.moderate")
    original = mod.PARAMS.copy()

    try:
        broken = original.copy()
        broken["STOP_LOSS_PCT"] = 0.0
        mod.PARAMS = broken

        if "config" in sys.modules:
            del sys.modules["config"]

        with pytest.raises(RuntimeError, match="Unsafe mode parameters"):
            import config as cfg
            importlib.reload(cfg)
    finally:
        mod.PARAMS = original
        if "config" in sys.modules:
            del sys.modules["config"]
