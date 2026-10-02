"""
Tests for execute_trade: mock Alpaca client and assert buy/sell/no-op decisions.
"""
import os
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import duckdb
import pytest

# Set before importing trading so TradingClient(...) does not raise (we mock it in tests)
os.environ["ALPACA_API_KEY"] = "test-key"
os.environ["ALPACA_SECRET_KEY"] = "test-secret"

from alpaca.trading.enums import OrderSide
import trading


def _patch_trade_limits():
    """Avoid touching real DB in tests: mock trade log and PDT/trail to allow trades."""
    return patch.multiple(
        trading,
        _count_daily=lambda: 0,
        _count_weekly=lambda: 0,
        _record_trade=lambda symbol, side, qty=0: None,
        _record_trade_history=lambda *a, **kw: None,
        _should_block_sell_pdt=lambda symbol: False,
        _would_sell_be_day_trade=lambda symbol: False,
        _count_day_trades_in_last_5_days=lambda: 0,
        _get_trail_running_high=lambda symbol: None,
        _set_trail_running_high=lambda symbol, running_high: None,
        _clear_trail_state=lambda symbol: None,
        # Allow discretionary TA sells in unit tests unless a test overrides this
        _should_block_sell_min_hold=lambda symbol: False,
        _last_buy_source=lambda symbol: None,
        MIN_HOLD_HOURS=0,
    )


def test_no_analysis_skips_trade():
    """None or empty analysis -> no order submitted."""
    with patch.object(trading, "trading_client") as mock_client:
        trading.execute_trade("TEST", None)
        mock_client.submit_order.assert_not_called()
    with patch.object(trading, "trading_client") as mock_client:
        trading.execute_trade("TEST", {})
        mock_client.submit_order.assert_not_called()


def test_no_strong_trend_skips_trade():
    """Analysis with strong_trend False -> no order submitted."""
    with patch.object(trading, "trading_client") as mock_client:
        analysis = {"strong_trend": False, "uptrend": True}
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_downtrend_stays_put_no_buy():
    """
    Downtrend-style analysis (no buy conditions) -> no BUY order.
    Bot does not 'buy the dip' by design. (Sell branch may run if we had a position; we mock no position.)
    """
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        analysis = {
            "strong_trend": True,   # ADX > 25 but could be downtrend
            "uptrend": False,
            "trending_up_a_lot": False,
            "near_upper_band": False,
            "sar_below_price": False,
            "bullish_crossover": False,
            "sar_flipped_to_bull": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "near_lower_band": True,
            "sar_above_price": True,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        # No BUY; sell branch runs but get_open_position raises so no order is submitted
        mock_client.submit_order.assert_not_called()


def test_all_buy_conditions_submits_buy():
    """When all buy conditions are True, submit_order(BUY) should be called."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_get_buying_power", return_value=100_000.0):
        mock_client.get_all_positions.return_value = []  # under max open positions
        analysis = {
            "strong_trend": True,
            "uptrend": True,
            "trending_up_a_lot": True,
            "near_upper_band": True,
            "sar_below_price": True,
            "bullish_crossover": True,
            "sar_flipped_to_bull": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "avoid_long": False,
            # New daily compensating filter flags (required by the current mode defaults in the BUY gate)
            "adx_rising": True,
            "volume_confirmed": True,
            "above_long_term_ma": True,
            "current_price": 100.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        # Entries are marketable limit orders, capped MAX_SLIPPAGE_PCT above the decision-time price.
        assert order.limit_price == pytest.approx(100.0 * (1 + trading.MAX_SLIPPAGE_PCT), abs=0.01)


def test_recent_bullish_signal_can_submit_buy():
    """A recent bullish confirmation should allow BUY even without same-bar crossover/flip."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_get_buying_power", return_value=100_000.0):
        mock_client.get_all_positions.return_value = []
        analysis = {
            "strong_trend": True,
            "uptrend": True,
            "trending_up_a_lot": True,
            "near_upper_band": True,
            "sar_below_price": True,
            "bullish_crossover": False,
            "sar_flipped_to_bull": False,
            "bullish_crossover_recent": True,
            "sar_flipped_to_bull_recent": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "avoid_long": False,
            # New daily compensating filter flags
            "adx_rising": True,
            "volume_confirmed": True,
            "above_long_term_ma": True,
            "current_price": 100.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY


def test_sell_condition_submits_sell_when_position_exists():
    """When sell conditions hold and we have a position, submit_order(SELL) should be called."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(qty=1)
        analysis = {
            "strong_trend": True,
            "near_lower_band": True,
            "sar_above_price": False,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
            "current_price": 100.0,
        }
        trading.execute_trade("TEST", analysis)
        # Should have called get_open_position and submit_order (sell)
        mock_client.get_open_position.assert_called_with("TEST")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.SELL
        # Discretionary exits are marketable limit orders, capped MAX_SLIPPAGE_PCT below the decision-time price.
        assert order.limit_price == pytest.approx(100.0 * (1 - trading.MAX_SLIPPAGE_PCT), abs=0.01)


def test_sell_condition_no_position_does_not_submit():
    """Sell conditions but no position -> no submit (or get_open_position raises)."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        analysis = {
            "strong_trend": True,
            "near_lower_band": True,
            "sar_above_price": False,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.get_open_position.assert_called_with("TEST")
        mock_client.submit_order.assert_not_called()


def test_avoid_long_blocks_buy():
    """When avoid_long is True (e.g. dead-cat bounce), do not BUY even if other conditions met."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_all_positions.return_value = []
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        analysis = {
            "strong_trend": True,
            "uptrend": True,
            "trending_up_a_lot": True,
            "near_upper_band": True,
            "sar_below_price": True,
            "bullish_crossover": True,
            "sar_flipped_to_bull": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "avoid_long": True,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_stop_loss_sells_when_below_threshold():
    """When position is down more than STOP_LOSS_PCT from entry, submit SELL."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        # Entry 100, current 94 -> 6% down; STOP_LOSS_PCT is 5%, so 94 <= 95 -> trigger
        mock_client.get_open_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        analysis = {
            "strong_trend": True,
            "current_price": 94.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.get_open_position.assert_called_with("TEST")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.SELL
        assert float(order.qty) == 1


def test_stop_loss_does_not_sell_when_above_threshold():
    """When position is down but less than STOP_LOSS_PCT, do not sell on stop-loss."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        # Entry 100, current 96 -> 4% down; 96 > 95 so no stop-loss
        mock_client.get_open_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        analysis = {
            "strong_trend": True,
            "current_price": 96.0,
            "trending_up_a_lot": False,
            "near_upper_band": False,
            "sar_below_price": False,
            "bullish_crossover": False,
            "sar_flipped_to_bull": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "near_lower_band": False,
            "sar_above_price": False,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        # Stop-loss not triggered; no other sell signal -> submit_order not called
        mock_client.submit_order.assert_not_called()


def _buy_conditions_for_notional():
    """Analysis dict that satisfies all BUY conditions (for notional/whole-share tests).
    Includes the new daily compensating filter flags so the tests pass with the current mode defaults.
    """
    return {
        "strong_trend": True,
        "uptrend": True,
        "trending_up_a_lot": True,
        "near_upper_band": True,
        "sar_below_price": True,
        "bullish_crossover": True,
        "sar_flipped_to_bull": False,
        "similar_to_yesterday": False,
        "bb_squeeze": False,
        "avoid_long": False,
        # New daily compensating filters (required by aggressive and some other modes by default)
        "adx_rising": True,
        "volume_confirmed": True,
        "above_long_term_ma": True,
    }


def test_notional_mode_buys_whole_share_when_price_le_notional():
    """When NOTIONAL_PER_TRADE is set and price <= notional and we have buying power, buy 1 whole share."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=100.0):
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _buy_conditions_for_notional()
        analysis["current_price"] = 50.0  # 50 <= 75, can afford 1 share
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "qty", None) == 1
        # Notional should not be set when we use qty
        assert getattr(order, "notional", None) is None


def test_notional_mode_buys_whole_share_when_price_equals_notional():
    """When price equals NOTIONAL_PER_TRADE and we have buying power, buy 1 whole share."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=100.0):
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _buy_conditions_for_notional()
        analysis["current_price"] = 75.0  # 75 <= 75
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "qty", None) == 1


def test_notional_mode_buys_notional_when_price_above_notional():
    """When NOTIONAL_PER_TRADE is set and price > notional, buy with notional (fractional)."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=100.0):
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _buy_conditions_for_notional()
        analysis["current_price"] = 200.0  # 200 > 75 -> use notional
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "notional", None) == 75.0


def test_notional_mode_skips_when_whole_share_unaffordable_uses_notional():
    """When price <= notional but buying_power < price, use notional (capped by buying power) instead of 1 share."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=30.0):  # Can't afford 1 share at 50
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _buy_conditions_for_notional()
        analysis["current_price"] = 50.0  # 50 <= 75 but buying_power=30 < 50
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        # Should use notional = min(75, 30) = 30
        assert getattr(order, "notional", None) == 30.0


def test_notional_mode_skips_when_buying_power_below_minimum():
    """When NOTIONAL_PER_TRADE is set but buying power < $1, skip BUY (Alpaca minimum)."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=0.5):
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _buy_conditions_for_notional()
        analysis["current_price"] = 200.0
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


# ─── PDT awareness ───


def test_pdt_does_not_block_stop_loss_sell():
    """Risk exits must still submit even when the PDT counter is full."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_pdt", return_value=True):
        mock_client.get_open_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        analysis = {
            "strong_trend": True,
            "current_price": 94.0,  # below stop-loss threshold
        }
        trading.execute_trade("TEST", analysis)
        mock_client.get_open_position.assert_called_with("TEST")
        mock_client.submit_order.assert_called_once()
        assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL


def test_pdt_blocks_signal_sell():
    """When PDT limit reached, signal-based SELL is skipped."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_pdt", return_value=True):
        mock_client.get_open_position.return_value = MagicMock(qty=1, avg_entry_price="100.0")
        analysis = {
            "strong_trend": True,
            "current_price": 98.0,  # above stop-loss so we don't hit that branch
            "near_lower_band": True,
            "sar_above_price": False,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.get_open_position.assert_called_with("TEST")
        mock_client.submit_order.assert_not_called()


def test_pdt_does_not_block_trailing_stop_sell():
    """Trailing stops are risk exits and must still submit under PDT pressure."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_pdt", return_value=True), \
         patch.object(trading, "_get_trail_running_high", return_value=110.0):
        mock_client.get_open_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        analysis = {
            "strong_trend": True,
            "current_price": 105.5,  # would trigger trailing stop (below 110*0.96)
            "trending_up_a_lot": False,
            "near_upper_band": False,
            "sar_below_price": False,
            "bullish_crossover": False,
            "sar_flipped_to_bull": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "near_lower_band": False,
            "sar_above_price": False,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL


# ─── Trailing stop ───


def test_trailing_stop_sells_when_active_and_price_drops_from_high():
    """When trail is active (price was 5%+ above entry) and price drops 4% from running high, submit SELL."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=2,
            avg_entry_price="100.0",
        )
        # Running high was 110; current now 105.5 -> 105.5 <= 110 * 0.96 = 105.6, so trail triggers
        with patch.object(trading, "_get_trail_running_high", return_value=110.0):
            analysis = {
                "strong_trend": True,
                "current_price": 105.5,
                "trending_up_a_lot": False,
                "near_upper_band": False,
                "sar_below_price": False,
                "bullish_crossover": False,
                "sar_flipped_to_bull": False,
                "similar_to_yesterday": False,
                "bb_squeeze": False,
                "near_lower_band": False,
                "sar_above_price": False,
                "sar_flipped_to_bear": False,
                "dive_bombing": False,
                "bearish_crossover": False,
            }
            trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.SELL
        assert float(order.qty) == 2


def test_trailing_stop_does_not_sell_when_not_activated():
    """When price is not yet 5% above entry, trailing stop is not active; no SELL from trail."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        # Current 103 = 3% above entry; trail activates at 105. No trail trigger.
        with patch.object(trading, "_get_trail_running_high", return_value=103.0):
            analysis = {
                "strong_trend": True,
                "current_price": 103.0,
                "trending_up_a_lot": False,
                "near_upper_band": False,
                "sar_below_price": False,
                "bullish_crossover": False,
                "sar_flipped_to_bull": False,
                "similar_to_yesterday": False,
                "bb_squeeze": False,
                "near_lower_band": False,
                "sar_above_price": False,
                "sar_flipped_to_bear": False,
                "dive_bombing": False,
                "bearish_crossover": False,
            }
            trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_trailing_stop_does_not_sell_when_price_has_not_dropped_enough():
    """When trail is active but price has not fallen 4% from running high, no SELL."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        # Running high 110, current 108. Trail active (108 >= 105). 108 <= 110*0.96=105.6? No.
        with patch.object(trading, "_get_trail_running_high", return_value=110.0):
            analysis = {
                "strong_trend": True,
                "current_price": 108.0,
                "trending_up_a_lot": False,
                "near_upper_band": False,
                "sar_below_price": False,
                "bullish_crossover": False,
                "sar_flipped_to_bull": False,
                "similar_to_yesterday": False,
                "bb_squeeze": False,
                "near_lower_band": False,
                "sar_above_price": False,
                "sar_flipped_to_bear": False,
                "dive_bombing": False,
                "bearish_crossover": False,
            }
            trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_bare_sar_above_price_does_not_sell_while_uptrend():
    """Price below SAR alone must not dump a position that is still in an uptrend (+DI > -DI)."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price="100.0"
        )
        analysis = {
            "strong_trend": True,
            "uptrend": True,
            "trending_up_a_lot": False,
            "current_price": 101.0,
            "near_lower_band": False,
            "sar_above_price": True,  # previously this alone forced a SELL
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "avoid_long": False,
            "sar_below_price": False,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_sar_above_plus_downtrend_does_sell():
    """SAR bearish regime + DI downtrend is a confirmed exit."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price="100.0"
        )
        analysis = {
            "strong_trend": True,
            "uptrend": False,
            "trending_up_a_lot": False,
            "current_price": 99.0,
            "near_lower_band": False,
            "sar_above_price": True,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL


def test_min_hold_blocks_ta_signal_sell_but_not_stop_loss():
    """MIN_HOLD_HOURS blocks discretionary TA exits; stop-loss still fires."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_min_hold", return_value=True):
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price="100.0"
        )
        analysis = {
            "strong_trend": True,
            "uptrend": False,
            "current_price": 99.0,
            "near_lower_band": False,
            "sar_above_price": True,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
            "trending_up_a_lot": False,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()

    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_min_hold", return_value=True), \
         patch.object(trading, "STOP_LOSS_PCT", 0.05):
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price="100.0"
        )
        analysis = {
            "strong_trend": True,
            "current_price": 94.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL


def test_stop_loss_fires_without_strong_trend():
    """Stops must protect capital even when ADX is weak (no strong_trend)."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "STOP_LOSS_PCT", 0.05):
        mock_client.get_open_position.return_value = MagicMock(
            qty=1, avg_entry_price="100.0"
        )
        analysis = {
            "strong_trend": False,
            "current_price": 94.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL


def test_buy_refuses_when_daily_count_unavailable():
    """A failed trade-log read must fail closed and refuse a new BUY."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_count_daily", return_value=None):
        mock_client.get_all_positions.return_value = []
        trading.execute_trade("TEST", _buy_conditions_for_notional())
        mock_client.submit_order.assert_not_called()


def test_live_host_mismatch_disables_order_submission():
    """Paper mode must not submit orders against a live Alpaca host override."""
    with patch.object(trading, "_base_url", "https://api.alpaca.markets"), \
         patch.object(trading, "TRADING_MODE", "moderate"):
        status = trading.get_trading_runtime_status()
        assert status["orders_allowed"] is False
        assert status["state"] == "misconfigured"


def test_dormant_mode_blocks_stop_loss_submit():
    """Dormant mode suppresses stop-loss orders in addition to buys."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "TRADING_MODE", "dormant"):
        mock_client.get_open_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        trading.execute_trade("TEST", {"strong_trend": True, "current_price": 94.0})
        mock_client.submit_order.assert_not_called()


def test_unfilled_stop_loss_sell_is_not_logged():
    """A submitted-but-unfilled exit must not consume a PDT/history slot locally."""
    db_file = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    db_file.close()
    os.unlink(db_file.name)

    unfilled_order = SimpleNamespace(
        id="order-1",
        status="new",
        filled_qty="0",
        filled_avg_price=None,
        qty="1",
    )
    try:
        with patch.object(trading, "DB_PATH", db_file.name), \
             patch.object(trading, "trading_client") as mock_client, \
             patch.object(trading, "_record_trade") as record_trade, \
             patch.object(trading, "_record_trade_history") as record_history:
            mock_client.get_open_position.return_value = MagicMock(
                qty=1,
                avg_entry_price="100.0",
            )
            mock_client.submit_order.return_value = unfilled_order
            mock_client.get_order_by_id.return_value = unfilled_order
            trading.execute_trade("TEST", {"strong_trend": True, "current_price": 94.0})
            record_trade.assert_not_called()
            record_history.assert_not_called()
    finally:
        if os.path.exists(db_file.name):
            os.unlink(db_file.name)


def test_locally_known_held_symbols_uses_net_filled_trade_log():
    """Local hold fallback should include only symbols with a positive net filled quantity."""
    db_file = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    db_file.close()
    os.unlink(db_file.name)
    con = duckdb.connect(db_file.name)
    try:
        con.execute(
            f"""
            CREATE TABLE {trading.TRADE_LOG_TABLE} (
                timestamp_utc DOUBLE,
                symbol VARCHAR,
                side VARCHAR,
                qty DOUBLE
            )
            """
        )
        con.execute(
            f"INSERT INTO {trading.TRADE_LOG_TABLE} VALUES "
            "(1, 'AAPL', 'BUY', 1), "
            "(2, 'AAPL', 'SELL', 1), "
            "(3, 'MSFT', 'BUY', 2), "
            "(4, 'TSLA', 'BUY', 0)"
        )
        con.close()

        with patch.object(trading, "DB_PATH", db_file.name):
            assert trading.get_locally_known_held_symbols() == {"MSFT", "TSLA"}
    finally:
        if os.path.exists(db_file.name):
            os.unlink(db_file.name)


def test_portfolio_open_risk_pct_sums_across_positions():
    """Open risk = sum(qty * entry_price * STOP_LOSS_PCT) / equity across every open position."""
    with patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "STOP_LOSS_PCT", 0.05):
        mock_client.get_all_positions.return_value = [
            SimpleNamespace(qty="10", avg_entry_price="100"),  # risk = 10*100*0.05 = 50
            SimpleNamespace(qty="4", avg_entry_price="50"),    # risk = 4*50*0.05 = 10
        ]
        # total risk = 60, equity = 1000 -> 6%
        assert trading._portfolio_open_risk_pct(1000.0) == pytest.approx(0.06)


def test_portfolio_open_risk_pct_zero_when_flat():
    """No open positions -> zero portfolio heat."""
    with patch.object(trading, "trading_client") as mock_client:
        mock_client.get_all_positions.return_value = []
        assert trading._portfolio_open_risk_pct(1000.0) == 0.0


def test_portfolio_open_risk_pct_none_on_api_failure():
    """Fail closed (None) when positions can't be read, rather than assuming zero risk."""
    with patch.object(trading, "trading_client") as mock_client:
        mock_client.get_all_positions.side_effect = Exception("boom")
        assert trading._portfolio_open_risk_pct(1000.0) is None


def test_portfolio_open_risk_pct_none_when_equity_non_positive():
    """Zero/negative equity can't be divided into a meaningful risk fraction."""
    with patch.object(trading, "trading_client"):
        assert trading._portfolio_open_risk_pct(0.0) is None


def test_entry_caps_blocks_buy_at_portfolio_heat_cap():
    """
    A new BUY is refused once existing open positions already consume the
    portfolio risk budget, even though MAX_OPEN_POSITIONS (a plain headcount)
    would still allow another position.
    """
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "STOP_LOSS_PCT", 0.05), \
         patch.object(trading, "MAX_PORTFOLIO_RISK_PCT", 0.03):
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_account.return_value = SimpleNamespace(equity="10000", buying_power="10000")
        # One existing position already risks 60*100*0.05 = 300 = 3% of 10000 equity -> at cap.
        mock_client.get_all_positions.return_value = [
            SimpleNamespace(qty="60", avg_entry_price="100"),
        ]
        analysis = {
            "strong_trend": True,
            "uptrend": True,
            "trending_up_a_lot": True,
            "near_upper_band": True,
            "sar_below_price": True,
            "bullish_crossover": True,
            "sar_flipped_to_bull": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "avoid_long": False,
            "adx_rising": True,
            "volume_confirmed": True,
            "above_long_term_ma": True,
            "current_price": 100.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


def test_entry_caps_allows_buy_under_portfolio_heat_cap():
    """Same setup, but existing open risk is well under the cap -> BUY proceeds."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "STOP_LOSS_PCT", 0.05), \
         patch.object(trading, "MAX_PORTFOLIO_RISK_PCT", 0.03):
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.get_account.return_value = SimpleNamespace(equity="10000", buying_power="10000")
        # Existing position risks only 1*100*0.05 = 5 = 0.05% of equity -> well under cap.
        mock_client.get_all_positions.return_value = [
            SimpleNamespace(qty="1", avg_entry_price="100"),
        ]
        analysis = {
            "strong_trend": True,
            "uptrend": True,
            "trending_up_a_lot": True,
            "near_upper_band": True,
            "sar_below_price": True,
            "bullish_crossover": True,
            "sar_flipped_to_bull": False,
            "similar_to_yesterday": False,
            "bb_squeeze": False,
            "avoid_long": False,
            "adx_rising": True,
            "volume_confirmed": True,
            "above_long_term_ma": True,
            "current_price": 100.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY


# ─── Correlation-aware position limits ───

def test_pearson_correlation_perfect_positive():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [2.0, 4.0, 6.0, 8.0, 10.0]
    assert trading._pearson_correlation(xs, ys) == pytest.approx(1.0)


def test_pearson_correlation_perfect_negative():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [10.0, 8.0, 6.0, 4.0, 2.0]
    assert trading._pearson_correlation(xs, ys) == pytest.approx(-1.0)


def test_pearson_correlation_none_when_no_variance():
    """A constant series has undefined correlation, not zero."""
    xs = [1.0, 1.0, 1.0, 1.0]
    ys = [1.0, 2.0, 3.0, 4.0]
    assert trading._pearson_correlation(xs, ys) is None


def test_pearson_correlation_needs_at_least_two_points():
    assert trading._pearson_correlation([1.0], [2.0]) is None


def test_max_correlation_with_held_picks_largest_absolute_value(monkeypatch):
    """-0.85 should beat 0.3 since we care about magnitude, not direction."""
    monkeypatch.setattr(trading, "_daily_returns", lambda symbol, lookback_days: {})
    def fake_corr(symbol_a, symbol_b, lookback_days, returns_a=None):
        return {"HELD1": 0.3, "HELD2": -0.85}.get(symbol_b)
    monkeypatch.setattr(trading, "_correlation", fake_corr)
    result = trading._max_correlation_with_held("NEW", {"HELD1", "HELD2"}, 60)
    assert result == -0.85


def test_max_correlation_with_held_skips_self(monkeypatch):
    monkeypatch.setattr(trading, "_daily_returns", lambda symbol, lookback_days: {})
    monkeypatch.setattr(trading, "_correlation", lambda a, b, lookback_days, returns_a=None: 1.0)
    assert trading._max_correlation_with_held("AAPL", {"AAPL"}, 60) is None


def test_max_correlation_with_held_none_when_all_unknown(monkeypatch):
    monkeypatch.setattr(trading, "_daily_returns", lambda symbol, lookback_days: {})
    monkeypatch.setattr(trading, "_correlation", lambda a, b, lookback_days, returns_a=None: None)
    assert trading._max_correlation_with_held("NEW", {"HELD1", "HELD2"}, 60) is None


def _make_trends_db(rows: list[tuple]) -> str:
    """Create a temp DuckDB file with a `trends` table seeded with `rows`."""
    db_file = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    db_file.close()
    os.unlink(db_file.name)
    con = duckdb.connect(db_file.name)
    try:
        con.execute("""
            CREATE TABLE trends (
                symbol VARCHAR, timestamp BIGINT,
                open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE
            )
        """)
        if rows:
            con.executemany("INSERT INTO trends VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    finally:
        con.close()
    return db_file.name


def _make_empty_db() -> str:
    """Path to a fresh, empty DuckDB file - tables are created on demand by _ensure_* helpers."""
    db_file = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    db_file.close()
    os.unlink(db_file.name)
    duckdb.connect(db_file.name).close()
    return db_file.name


def test_daily_returns_empty_for_unknown_symbol():
    db_path = _make_trends_db([])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._daily_returns("ZZZZ", lookback_days=30) == {}
    finally:
        os.unlink(db_path)


def test_daily_returns_and_correlation_from_trends_table():
    """Two symbols with identical price paths must come back perfectly correlated."""
    rows = []
    for i in range(25):
        ts = i * 86400
        price = float(100 + i)
        rows.append(("AAA", ts, price, price, price, price, 1000.0))
        rows.append(("BBB", ts, price, price, price, price, 1000.0))
    db_path = _make_trends_db(rows)
    try:
        with patch.object(trading, "DB_PATH", db_path):
            returns_a = trading._daily_returns("AAA", lookback_days=24)
            assert len(returns_a) == 24
            assert trading._correlation("AAA", "BBB", lookback_days=24) == pytest.approx(1.0)
    finally:
        os.unlink(db_path)


def test_correlation_none_below_min_samples():
    """Fewer overlapping bars than CORRELATION_MIN_SAMPLES -> unknown, not blocked."""
    rows = []
    for i in range(5):  # 5 bars -> 4 daily returns, well under the default 20-sample floor
        ts = i * 86400
        price = float(100 + i)
        rows.append(("AAA", ts, price, price, price, price, 1000.0))
        rows.append(("BBB", ts, price, price, price, price, 1000.0))
    db_path = _make_trends_db(rows)
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._correlation("AAA", "BBB", lookback_days=60) is None
    finally:
        os.unlink(db_path)


def _full_buy_analysis_at(price: float) -> dict:
    return {
        "strong_trend": True,
        "uptrend": True,
        "trending_up_a_lot": True,
        "near_upper_band": True,
        "sar_below_price": True,
        "bullish_crossover": True,
        "sar_flipped_to_bull": False,
        "similar_to_yesterday": False,
        "bb_squeeze": False,
        "avoid_long": False,
        "adx_rising": True,
        "volume_confirmed": True,
        "above_long_term_ma": True,
        "current_price": price,
    }


def test_entry_caps_blocks_buy_correlated_with_held_position():
    """A new BUY is refused when its recent returns are highly correlated with an already-held symbol."""
    rows = []
    for i in range(25):
        ts = i * 86400
        price = float(100 + i)
        rows.append(("NEW", ts, price, price, price, price, 1000.0))
        rows.append(("HELD", ts, price, price, price, price, 1000.0))  # identical path -> corr = 1.0
    db_path = _make_trends_db(rows)
    try:
        with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
             patch.object(trading, "DB_PATH", db_path), \
             patch.object(trading, "STOP_LOSS_PCT", 0.05), \
             patch.object(trading, "MAX_PORTFOLIO_RISK_PCT", 0.50), \
             patch.object(trading, "MAX_POSITION_CORRELATION", 0.9), \
             patch.object(trading, "CORRELATION_LOOKBACK_DAYS", 24):
            mock_client.get_open_position.side_effect = Exception("position does not exist")
            mock_client.get_account.return_value = SimpleNamespace(equity="10000", buying_power="10000")
            mock_client.get_all_positions.return_value = [
                SimpleNamespace(qty="1", avg_entry_price="100", symbol="HELD"),
            ]
            trading.execute_trade("NEW", _full_buy_analysis_at(100.0))
            mock_client.submit_order.assert_not_called()
    finally:
        os.unlink(db_path)


def test_entry_caps_allows_buy_when_correlation_under_cap():
    """Same setup, but the correlation cap is wide enough that the trade still proceeds."""
    rows = []
    for i in range(25):
        ts = i * 86400
        price = float(100 + i)
        rows.append(("NEW", ts, price, price, price, price, 1000.0))
        rows.append(("HELD", ts, price, price, price, price, 1000.0))
    db_path = _make_trends_db(rows)
    try:
        with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
             patch.object(trading, "DB_PATH", db_path), \
             patch.object(trading, "STOP_LOSS_PCT", 0.05), \
             patch.object(trading, "MAX_PORTFOLIO_RISK_PCT", 0.50), \
             patch.object(trading, "MAX_POSITION_CORRELATION", 1.01), \
             patch.object(trading, "CORRELATION_LOOKBACK_DAYS", 24):
            mock_client.get_open_position.side_effect = Exception("position does not exist")
            mock_client.get_account.return_value = SimpleNamespace(equity="10000", buying_power="10000")
            mock_client.get_all_positions.return_value = [
                SimpleNamespace(qty="1", avg_entry_price="100", symbol="HELD"),
            ]
            trading.execute_trade("NEW", _full_buy_analysis_at(100.0))
            mock_client.submit_order.assert_called_once()
            order = mock_client.submit_order.call_args[0][0]
            assert order.side == OrderSide.BUY
    finally:
        os.unlink(db_path)


# ─── Execution quality: slippage-capped limit orders ───

def test_build_qty_order_buy_caps_above_reference_price():
    with patch.object(trading, "MAX_SLIPPAGE_PCT", 0.01):
        order = trading._build_qty_order("AAPL", OrderSide.BUY, 10, 100.0)
        assert order.limit_price == pytest.approx(101.0)
        assert order.qty == 10


def test_build_qty_order_sell_caps_below_reference_price():
    with patch.object(trading, "MAX_SLIPPAGE_PCT", 0.01):
        order = trading._build_qty_order("AAPL", OrderSide.SELL, 10, 100.0)
        assert order.limit_price == pytest.approx(99.0)


def test_build_qty_order_none_without_reference_price():
    """No usable price -> no order, rather than submitting one with no anchor."""
    assert trading._build_qty_order("AAPL", OrderSide.BUY, 1, None) is None
    assert trading._build_qty_order("AAPL", OrderSide.BUY, 1, 0) is None
    assert trading._build_qty_order("AAPL", OrderSide.BUY, 1, -5.0) is None


def test_log_fill_slippage_warns_and_alerts_past_cap(monkeypatch):
    alerts = []
    monkeypatch.setattr(trading, "send_alert", lambda msg, kind: alerts.append((msg, kind)))
    with patch.object(trading, "MAX_SLIPPAGE_PCT", 0.01):
        # Bought at 103 vs an expected 100 -> 3% adverse slippage, well past the 1% cap.
        trading._log_fill_slippage("AAPL", "BUY", "signal", 100.0, 103.0)
    assert len(alerts) == 1
    assert "AAPL" in alerts[0][0]
    assert alerts[0][1] == "error"


def test_log_fill_slippage_silent_within_cap(monkeypatch):
    alerts = []
    monkeypatch.setattr(trading, "send_alert", lambda msg, kind: alerts.append((msg, kind)))
    with patch.object(trading, "MAX_SLIPPAGE_PCT", 0.01):
        trading._log_fill_slippage("AAPL", "BUY", "signal", 100.0, 100.2)
    assert alerts == []


def test_log_fill_slippage_silent_on_favorable_buy_fill(monkeypatch):
    """A BUY that fills BELOW the expected price is favorable, not adverse slippage."""
    alerts = []
    monkeypatch.setattr(trading, "send_alert", lambda msg, kind: alerts.append((msg, kind)))
    with patch.object(trading, "MAX_SLIPPAGE_PCT", 0.01):
        trading._log_fill_slippage("AAPL", "BUY", "signal", 100.0, 97.0)
    assert alerts == []


def test_log_fill_slippage_direction_aware_for_sells(monkeypatch):
    """A SELL that fills ABOVE the expected price is favorable, not adverse slippage."""
    alerts = []
    monkeypatch.setattr(trading, "send_alert", lambda msg, kind: alerts.append((msg, kind)))
    with patch.object(trading, "MAX_SLIPPAGE_PCT", 0.01):
        trading._log_fill_slippage("AAPL", "SELL", "signal", 100.0, 103.0)
    assert alerts == []


# ─── Regime switching: mean-reversion counter-strategy ───

def test_mean_reversion_regime_true_when_choppy_and_safe():
    with patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15):
        analysis = {"strong_trend": False, "avoid_long": False, "adx": 8.0}
        assert trading._mean_reversion_regime(analysis) is True


def test_mean_reversion_regime_false_when_trending():
    with patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15):
        analysis = {"strong_trend": True, "avoid_long": False, "adx": 8.0}
        assert trading._mean_reversion_regime(analysis) is False


def test_mean_reversion_regime_false_when_avoid_long():
    """Dead-cat bounce / extended decline / volatility spike mean something is
    wrong, not that the market is calmly ranging - never mean-revert into those."""
    with patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15):
        analysis = {"strong_trend": False, "avoid_long": True, "adx": 8.0}
        assert trading._mean_reversion_regime(analysis) is False


def test_mean_reversion_regime_false_in_dead_zone():
    """ADX below the trend threshold but not clearly below the chop ceiling -> neither strategy fires."""
    with patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15):
        analysis = {"strong_trend": False, "avoid_long": False, "adx": 16.5}
        assert trading._mean_reversion_regime(analysis) is False


def test_mean_reversion_regime_false_when_adx_missing():
    with patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15):
        assert trading._mean_reversion_regime({"strong_trend": False, "avoid_long": False, "adx": None}) is False


def test_mean_reversion_buy_signal_requires_rsi_and_band():
    with patch.object(trading, "MEAN_REVERSION_RSI_OVERSOLD", 30):
        assert trading._mean_reversion_buy_signal({"rsi_14": 25.0, "near_lower_band": True}) is True
        assert trading._mean_reversion_buy_signal({"rsi_14": 35.0, "near_lower_band": True}) is False
        assert trading._mean_reversion_buy_signal({"rsi_14": 25.0, "near_lower_band": False}) is False
        assert trading._mean_reversion_buy_signal({"rsi_14": None, "near_lower_band": True}) is False


def test_mean_reversion_sell_signal_on_rsi_or_band():
    with patch.object(trading, "MEAN_REVERSION_RSI_OVERBOUGHT", 70):
        assert trading._mean_reversion_sell_signal({"rsi_14": 75.0, "near_upper_band": False}) is True
        assert trading._mean_reversion_sell_signal({"rsi_14": 50.0, "near_upper_band": True}) is True
        assert trading._mean_reversion_sell_signal({"rsi_14": 50.0, "near_upper_band": False}) is False


def test_compute_mean_reversion_qty_uses_own_risk_and_stop():
    with patch.object(trading, "MEAN_REVERSION_RISK_PCT_PER_TRADE", 0.005), \
         patch.object(trading, "MEAN_REVERSION_STOP_LOSS_PCT", 0.035), \
         patch.object(trading, "MAX_POSITION_PCT_EQUITY", 0.10), \
         patch.object(trading, "MAX_SHARES", 100), \
         patch.object(trading, "MIN_SHARES", 1):
        analysis = {"current_price": 50.0}
        # risk_amount=50, stop_distance=1.75 -> position_value=1428.57 -> qty=28,
        # capped by MAX_POSITION_PCT_EQUITY (1000/50=20) -> 20
        qty = trading._compute_mean_reversion_qty(analysis, 10_000.0)
        assert qty == 20


def test_compute_mean_reversion_qty_falls_back_to_min_shares():
    with patch.object(trading, "MEAN_REVERSION_RISK_PCT_PER_TRADE", 0.005):
        assert trading._compute_mean_reversion_qty({"current_price": 0}, 10_000.0) == trading.MIN_SHARES
        assert trading._compute_mean_reversion_qty({"current_price": 50.0}, 0) == trading.MIN_SHARES


def test_last_buy_source_reads_most_recent_buy():
    db_path = _make_trends_db([])
    try:
        con = duckdb.connect(db_path)
        con.execute("""
            CREATE TABLE trade_history (
                timestamp_utc DOUBLE, symbol VARCHAR, side VARCHAR,
                qty DOUBLE, price DOUBLE, source VARCHAR
            )
        """)
        con.execute(
            "INSERT INTO trade_history VALUES "
            "(1, 'AAPL', 'BUY', 1, 100.0, 'ta'), "
            "(2, 'AAPL', 'SELL', 1, 110.0, 'ta'), "
            "(3, 'AAPL', 'BUY', 1, 90.0, 'mean_reversion')"
        )
        con.close()
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._last_buy_source("AAPL") == "mean_reversion"
            assert trading._last_buy_source("ZZZZ") is None
    finally:
        os.unlink(db_path)


def test_last_buy_source_none_on_db_failure():
    """Never raise - exit-rule selection must degrade gracefully, not crash _try_risk_exit."""
    with patch.object(trading, "DB_PATH", "md:?motherduck_token=invalid-for-test"):
        assert trading._last_buy_source("AAPL") is None


# ─── Wash-sale flag ───

def _make_trade_history_db(rows: list[tuple]) -> str:
    """Temp DuckDB file with a trade_history table seeded with rows."""
    db_path = _make_trends_db([])
    con = duckdb.connect(db_path)
    try:
        con.execute("""
            CREATE TABLE trade_history (
                timestamp_utc DOUBLE, symbol VARCHAR, side VARCHAR,
                qty DOUBLE, price DOUBLE, source VARCHAR
            )
        """)
        if rows:
            con.executemany("INSERT INTO trade_history VALUES (?, ?, ?, ?, ?, ?)", rows)
    finally:
        con.close()
    return db_path


def test_fifo_cost_basis_single_lot():
    db_path = _make_trade_history_db([
        (1.0, "AAPL", "BUY", 10.0, 100.0, "ta"),
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._fifo_cost_basis("AAPL", before_ts=100.0, qty_needed=10.0) == pytest.approx(100.0)
    finally:
        os.unlink(db_path)


def test_fifo_cost_basis_averages_across_multiple_buys_oldest_first():
    db_path = _make_trade_history_db([
        (1.0, "AAPL", "BUY", 10.0, 100.0, "ta"),
        (2.0, "AAPL", "BUY", 10.0, 120.0, "ta"),
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            # Needs 15 shares: all 10 @ 100 + 5 @ 120 -> (1000 + 600) / 15
            assert trading._fifo_cost_basis("AAPL", before_ts=100.0, qty_needed=15.0) == pytest.approx(1600.0 / 15)
    finally:
        os.unlink(db_path)


def test_fifo_cost_basis_consumes_lots_on_prior_sells():
    db_path = _make_trade_history_db([
        (1.0, "AAPL", "BUY", 10.0, 100.0, "ta"),   # fully sold below
        (2.0, "AAPL", "SELL", 10.0, 105.0, "ta"),
        (3.0, "AAPL", "BUY", 10.0, 150.0, "ta"),   # the only open lot by before_ts
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._fifo_cost_basis("AAPL", before_ts=100.0, qty_needed=10.0) == pytest.approx(150.0)
    finally:
        os.unlink(db_path)


def test_fifo_cost_basis_none_when_insufficient_history():
    db_path = _make_trade_history_db([
        (1.0, "AAPL", "BUY", 5.0, 100.0, "ta"),
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._fifo_cost_basis("AAPL", before_ts=100.0, qty_needed=10.0) is None
    finally:
        os.unlink(db_path)


def test_wash_sale_risk_flags_rebuy_after_recent_loss():
    now = time.time()
    db_path = _make_trade_history_db([
        (now - 10 * 86400, "AAPL", "BUY", 10.0, 100.0, "ta"),
        (now - 5 * 86400, "AAPL", "SELL", 10.0, 90.0, "ta"),  # loss of $100
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            risk = trading._wash_sale_risk("AAPL", buy_ts=now)
        assert risk is not None
        assert risk["loss_amount"] == pytest.approx(100.0)
        assert risk["sell_price"] == pytest.approx(90.0)
        assert risk["cost_basis"] == pytest.approx(100.0)
    finally:
        os.unlink(db_path)


def test_wash_sale_risk_none_when_prior_sale_was_a_gain():
    now = time.time()
    db_path = _make_trade_history_db([
        (now - 10 * 86400, "AAPL", "BUY", 10.0, 100.0, "ta"),
        (now - 5 * 86400, "AAPL", "SELL", 10.0, 110.0, "ta"),  # gain, not a loss
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._wash_sale_risk("AAPL", buy_ts=now) is None
    finally:
        os.unlink(db_path)


def test_wash_sale_risk_none_when_sale_outside_window():
    now = time.time()
    db_path = _make_trade_history_db([
        (now - 40 * 86400, "AAPL", "BUY", 10.0, 100.0, "ta"),
        (now - 35 * 86400, "AAPL", "SELL", 10.0, 90.0, "ta"),  # loss, but > 30 days ago
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._wash_sale_risk("AAPL", buy_ts=now) is None
    finally:
        os.unlink(db_path)


def test_wash_sale_risk_none_when_no_prior_sale():
    db_path = _make_trade_history_db([])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._wash_sale_risk("AAPL", buy_ts=time.time()) is None
    finally:
        os.unlink(db_path)


def test_wash_sale_risk_none_when_cost_basis_unknown():
    """A loss-looking sale with no matching buy history must not guess -> no flag."""
    now = time.time()
    db_path = _make_trade_history_db([
        (now - 5 * 86400, "AAPL", "SELL", 10.0, 90.0, "ta"),  # no prior BUY at all
    ])
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._wash_sale_risk("AAPL", buy_ts=now) is None
    finally:
        os.unlink(db_path)


def test_check_wash_sale_risk_alerts_when_flagged(monkeypatch):
    alerts = []
    monkeypatch.setattr(trading, "send_alert", lambda msg, kind: alerts.append((msg, kind)))
    monkeypatch.setattr(
        trading, "_wash_sale_risk",
        lambda symbol, buy_ts: {"sell_date_et": "2026-01-01", "sell_price": 90.0, "cost_basis": 100.0, "loss_amount": 100.0},
    )
    trading._check_wash_sale_risk("AAPL", time.time())
    assert len(alerts) == 1
    assert "wash sale" in alerts[0][0].lower()
    assert "AAPL" in alerts[0][0]
    assert alerts[0][1] == "error"


def test_check_wash_sale_risk_silent_when_not_flagged(monkeypatch):
    alerts = []
    monkeypatch.setattr(trading, "send_alert", lambda msg, kind: alerts.append((msg, kind)))
    monkeypatch.setattr(trading, "_wash_sale_risk", lambda symbol, buy_ts: None)
    trading._check_wash_sale_risk("AAPL", time.time())
    assert alerts == []


def test_record_order_fill_delta_checks_wash_sale_only_on_buy(monkeypatch):
    calls = []
    monkeypatch.setattr(trading, "_check_wash_sale_risk", lambda symbol, buy_ts: calls.append(symbol))
    with patch.object(trading, "_record_trade"), \
         patch.object(trading, "_record_trade_history"):
        order = SimpleNamespace(filled_qty="1", filled_avg_price="100.0")
        trading._record_order_fill_delta("AAPL", "BUY", "ta", 100.0, order, 0.0)
        trading._record_order_fill_delta("AAPL", "SELL", "ta", 100.0, order, 0.0)
    assert calls == ["AAPL"]


def test_try_risk_exit_uses_tighter_stop_for_mean_reversion_position():
    """A mean-reversion position must stop out at MEAN_REVERSION_STOP_LOSS_PCT, not the wider trend STOP_LOSS_PCT."""
    with patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_last_buy_source", return_value=trading.MEAN_REVERSION_SOURCE), \
         patch.object(trading, "STOP_LOSS_PCT", 0.10), \
         patch.object(trading, "MEAN_REVERSION_STOP_LOSS_PCT", 0.03):
        mock_client.get_open_position.return_value = MagicMock(qty=1, avg_entry_price="100.0")
        # 95 is within the 10% trend stop but breaches the tighter 3% mean-reversion stop.
        analysis = {"current_price": 95.0}
        assert trading._try_risk_exit("TEST", analysis) is True
        mock_client.submit_order.assert_called_once()


def test_try_risk_exit_skips_trailing_stop_for_mean_reversion_position():
    """Mean-reversion takes profit at its own target, not via a trailing stop."""
    with patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_last_buy_source", return_value=trading.MEAN_REVERSION_SOURCE), \
         patch.object(trading, "STOP_LOSS_PCT", 0.10), \
         patch.object(trading, "MEAN_REVERSION_STOP_LOSS_PCT", 0.03), \
         patch.object(trading, "TRAIL_ACTIVATION_PCT", 0.01), \
         patch.object(trading, "TRAIL_PCT", 0.01), \
         patch.object(trading, "_get_trail_running_high", return_value=110.0):
        mock_client.get_open_position.return_value = MagicMock(qty=1, avg_entry_price="100.0")
        # Price is above the mean-reversion stop and would normally trigger a
        # trend trailing-stop (way below running_high), but must not for a
        # mean-reversion position.
        analysis = {"current_price": 105.0}
        assert trading._try_risk_exit("TEST", analysis) is False
        mock_client.submit_order.assert_not_called()


def _choppy_analysis(price=50.0, rsi=20.0, near_lower=True, near_upper=False, adx=8.0):
    return {
        "strong_trend": False,
        "avoid_long": False,
        "adx": adx,
        "rsi_14": rsi,
        "near_lower_band": near_lower,
        "near_upper_band": near_upper,
        "current_price": price,
    }


def test_mean_reversion_disabled_by_default_takes_no_action():
    """With MEAN_REVERSION_ENABLED left at its real (False) per-mode value, a
    textbook oversold setup must still produce no order - today's behavior."""
    with patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_trade("TEST", _choppy_analysis())
        mock_client.submit_order.assert_not_called()


def test_try_mean_reversion_buy_submits_with_mean_reversion_source():
    with patch.object(trading, "MEAN_REVERSION_ENABLED", True), \
         patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15), \
         patch.object(trading, "MEAN_REVERSION_RSI_OVERSOLD", 30), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_entry_caps_allow_buy", return_value=True), \
         patch.object(trading, "_would_sell_be_day_trade", return_value=False), \
         patch.object(trading, "_get_account_equity", return_value=10_000.0), \
         patch.object(trading, "_get_buying_power", return_value=10_000.0), \
         patch.object(trading, "_submit_order", return_value=(True, True)) as mock_submit:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_trade("TEST", _choppy_analysis())
        mock_submit.assert_called_once()
        kwargs = mock_submit.call_args.kwargs
        assert kwargs["side"] == "BUY"
        assert kwargs["source"] == trading.MEAN_REVERSION_SOURCE
        assert kwargs["order"].side == OrderSide.BUY


def test_try_mean_reversion_no_signal_in_dead_zone_returns_none():
    """ADX between the chop ceiling and the trend threshold -> no action, no 'no signal' report either."""
    with patch.object(trading, "MEAN_REVERSION_ENABLED", True), \
         patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        result = trading.execute_trade("TEST", _choppy_analysis(adx=16.0))
        mock_client.submit_order.assert_not_called()
        assert result is None


def test_try_mean_reversion_choppy_no_signal_reports_truthy():
    """Choppy regime but RSI hasn't hit oversold yet -> reported like the trend branch's scorecard, for the no-signal digest."""
    with patch.object(trading, "MEAN_REVERSION_ENABLED", True), \
         patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15), \
         patch.object(trading, "MEAN_REVERSION_RSI_OVERSOLD", 30), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        result = trading.execute_trade("TEST", _choppy_analysis(rsi=45.0))
        mock_client.submit_order.assert_not_called()
        assert result


def test_try_mean_reversion_ignores_position_held_by_trend_strategy():
    """Chop + held position opened by the trend strategy -> this strategy leaves it alone entirely."""
    with patch.object(trading, "MEAN_REVERSION_ENABLED", True), \
         patch.object(trading, "MEAN_REVERSION_ADX_CEILING", 15), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_last_buy_source", return_value="ta"):
        mock_client.get_open_position.return_value = MagicMock(qty=1, avg_entry_price="100.0")
        # Price close to entry so the trend strategy's own stop-loss doesn't
        # fire either - isolating that _try_mean_reversion itself takes no action.
        trading.execute_trade("TEST", _choppy_analysis(price=98.0))
        mock_client.submit_order.assert_not_called()


def test_try_mean_reversion_sells_own_position_on_target_hit():
    with patch.object(trading, "MEAN_REVERSION_ENABLED", True), \
         patch.object(trading, "MEAN_REVERSION_RSI_OVERBOUGHT", 70), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_last_buy_source", return_value=trading.MEAN_REVERSION_SOURCE), \
         patch.object(trading, "_submit_order", return_value=(True, True)) as mock_submit:
        mock_client.get_open_position.return_value = MagicMock(qty=2, avg_entry_price="90.0")
        analysis = _choppy_analysis(price=100.0, rsi=75.0, near_lower=False)
        trading.execute_trade("TEST", analysis)
        mock_submit.assert_called_once()
        kwargs = mock_submit.call_args.kwargs
        assert kwargs["side"] == "SELL"
        assert kwargs["source"] == trading.MEAN_REVERSION_SOURCE
        assert float(kwargs["order"].qty) == 2


def test_try_mean_reversion_holds_own_position_when_target_not_hit():
    with patch.object(trading, "MEAN_REVERSION_ENABLED", True), \
         patch.object(trading, "MEAN_REVERSION_RSI_OVERBOUGHT", 70), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_last_buy_source", return_value=trading.MEAN_REVERSION_SOURCE):
        mock_client.get_open_position.return_value = MagicMock(qty=2, avg_entry_price="90.0")
        analysis = _choppy_analysis(price=95.0, rsi=55.0, near_lower=False, near_upper=False)
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


# ─── Circuit breaker ───

def test_circuit_breaker_tripped_when_drawdown_meets_threshold():
    with patch.object(trading, "CIRCUIT_BREAKER_ENABLED", True), \
         patch.object(trading, "CIRCUIT_BREAKER_DRAWDOWN_PCT", 0.05):
        # 10000 -> 9500 is exactly 5% down
        assert trading._circuit_breaker_tripped(9500.0, 10000.0) is True


def test_circuit_breaker_not_tripped_under_threshold():
    with patch.object(trading, "CIRCUIT_BREAKER_ENABLED", True), \
         patch.object(trading, "CIRCUIT_BREAKER_DRAWDOWN_PCT", 0.05):
        assert trading._circuit_breaker_tripped(9600.0, 10000.0) is False


def test_circuit_breaker_disabled_never_trips():
    with patch.object(trading, "CIRCUIT_BREAKER_ENABLED", False), \
         patch.object(trading, "CIRCUIT_BREAKER_DRAWDOWN_PCT", 0.05):
        assert trading._circuit_breaker_tripped(1000.0, 10000.0) is False


def test_circuit_breaker_fails_open_without_baseline():
    """No open-equity snapshot yet (e.g. first minutes after startup) -> not tripped, not blocked."""
    with patch.object(trading, "CIRCUIT_BREAKER_ENABLED", True), \
         patch.object(trading, "CIRCUIT_BREAKER_DRAWDOWN_PCT", 0.05):
        assert trading._circuit_breaker_tripped(100.0, None) is False
        assert trading._circuit_breaker_tripped(100.0, 0) is False


def test_todays_open_equity_returns_none_when_not_yet_captured():
    db_path = _make_empty_db()
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._todays_open_equity() is None
    finally:
        os.unlink(db_path)


def test_todays_open_equity_reads_todays_open_snapshot():
    db_path = _make_empty_db()
    try:
        today_et = trading.datetime.now(trading._ET).strftime("%Y-%m-%d")
        con = duckdb.connect(db_path)
        con.execute("""
            CREATE TABLE portfolio_snapshots (
                timestamp_utc DOUBLE, date_et VARCHAR, label VARCHAR, equity DOUBLE
            )
        """)
        con.execute(
            "INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?), (?, ?, ?, ?)",
            [1.0, today_et, "open", 10000.0, 2.0, "2000-01-01", "open", 1.0],
        )
        con.close()
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._todays_open_equity() == 10000.0
    finally:
        os.unlink(db_path)


def test_claim_circuit_breaker_alert_once_per_day():
    db_path = _make_empty_db()
    try:
        with patch.object(trading, "DB_PATH", db_path):
            assert trading._claim_circuit_breaker_alert_for_today() is True
            assert trading._claim_circuit_breaker_alert_for_today() is False
            assert trading._claim_circuit_breaker_alert_for_today() is False
    finally:
        os.unlink(db_path)


def test_entry_caps_blocks_buy_when_circuit_breaker_tripped():
    """A tripped circuit breaker refuses the BUY and alerts exactly once."""
    db_path = _make_empty_db()
    try:
        with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
             patch.object(trading, "DB_PATH", db_path), \
             patch.object(trading, "CIRCUIT_BREAKER_ENABLED", True), \
             patch.object(trading, "CIRCUIT_BREAKER_DRAWDOWN_PCT", 0.05), \
             patch.object(trading, "send_alert") as mock_alert:
            mock_client.get_open_position.side_effect = Exception("position does not exist")
            mock_client.get_all_positions.return_value = []
            # equity 9000 vs open 10000 -> 10% drawdown, well past the 5% threshold
            mock_client.get_account.return_value = SimpleNamespace(equity="9000", buying_power="9000")
            today_et = trading.datetime.now(trading._ET).strftime("%Y-%m-%d")
            con = duckdb.connect(db_path)
            con.execute("""
                CREATE TABLE portfolio_snapshots (
                    timestamp_utc DOUBLE, date_et VARCHAR, label VARCHAR, equity DOUBLE
                )
            """)
            con.execute(
                "INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?)", [1.0, today_et, "open", 10000.0]
            )
            con.close()

            trading.execute_trade("TEST", _full_buy_analysis_at(100.0))
            mock_client.submit_order.assert_not_called()
            assert mock_alert.call_count == 1
            assert "Circuit breaker" in mock_alert.call_args[0][0]

            # A second blocked symbol the same day must not alert again.
            trading.execute_trade("OTHER", _full_buy_analysis_at(100.0))
            mock_client.submit_order.assert_not_called()
            assert mock_alert.call_count == 1
    finally:
        os.unlink(db_path)


def test_entry_caps_allows_buy_when_drawdown_under_threshold():
    db_path = _make_empty_db()
    try:
        with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
             patch.object(trading, "DB_PATH", db_path), \
             patch.object(trading, "CIRCUIT_BREAKER_ENABLED", True), \
             patch.object(trading, "CIRCUIT_BREAKER_DRAWDOWN_PCT", 0.05), \
             patch.object(trading, "MAX_PORTFOLIO_RISK_PCT", 0.50):
            mock_client.get_open_position.side_effect = Exception("position does not exist")
            mock_client.get_all_positions.return_value = []
            # equity 9800 vs open 10000 -> 2% drawdown, under the 5% threshold
            mock_client.get_account.return_value = SimpleNamespace(equity="9800", buying_power="9800")
            today_et = trading.datetime.now(trading._ET).strftime("%Y-%m-%d")
            con = duckdb.connect(db_path)
            con.execute("""
                CREATE TABLE portfolio_snapshots (
                    timestamp_utc DOUBLE, date_et VARCHAR, label VARCHAR, equity DOUBLE
                )
            """)
            con.execute(
                "INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?)", [1.0, today_et, "open", 10000.0]
            )
            con.close()

            trading.execute_trade("TEST", _full_buy_analysis_at(100.0))
            mock_client.submit_order.assert_called_once()
    finally:
        os.unlink(db_path)


def test_ta_sell_signal_helper():
    """Unit-check the confirmed-exit combinator used by execute_trade."""
    assert trading._ta_sell_signal({"dive_bombing": True}) is True
    assert trading._ta_sell_signal({"sar_flipped_to_bear": True}) is True
    assert trading._ta_sell_signal({"sar_above_price": True, "uptrend": True}) is False
    assert trading._ta_sell_signal({"sar_above_price": True, "uptrend": False}) is True
    assert trading._ta_sell_signal(
        {"near_lower_band": True, "uptrend": True, "sar_above_price": False}
    ) is False
    assert trading._ta_sell_signal(
        {"near_lower_band": True, "uptrend": False}
    ) is True
