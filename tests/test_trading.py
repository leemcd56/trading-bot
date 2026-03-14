"""
Tests for execute_trade: mock Alpaca client and assert buy/sell/no-op decisions.
"""
import os
from unittest.mock import patch, MagicMock

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
        _should_block_sell_pdt=lambda symbol: False,
        _count_day_trades_in_last_5_days=lambda: 0,
        _get_trail_running_high=lambda symbol: None,
        _set_trail_running_high=lambda symbol, running_high: None,
        _clear_trail_state=lambda symbol: None,
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
        mock_client.get_position.side_effect = Exception("position does not exist")
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
        # No BUY; sell branch runs but get_position raises so no order is submitted
        mock_client.submit_order.assert_not_called()


def test_all_buy_conditions_submits_buy():
    """When all buy conditions are True, submit_order(BUY) should be called."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
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
        }
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY


def test_sell_condition_submits_sell_when_position_exists():
    """When sell conditions hold and we have a position, submit_order(SELL) should be called."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_position.return_value = MagicMock(qty=1)
        analysis = {
            "strong_trend": True,
            "near_lower_band": True,
            "sar_above_price": False,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        # Should have called get_position and submit_order (sell)
        mock_client.get_position.assert_called_with("TEST")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.SELL


def test_sell_condition_no_position_does_not_submit():
    """Sell conditions but no position -> no submit (or get_position raises)."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_position.side_effect = Exception("position does not exist")
        analysis = {
            "strong_trend": True,
            "near_lower_band": True,
            "sar_above_price": False,
            "sar_flipped_to_bear": False,
            "dive_bombing": False,
            "bearish_crossover": False,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.get_position.assert_called_with("TEST")
        mock_client.submit_order.assert_not_called()


def test_avoid_long_blocks_buy():
    """When avoid_long is True (e.g. dead-cat bounce), do not BUY even if other conditions met."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_all_positions.return_value = []
        mock_client.get_position.side_effect = Exception("position does not exist")
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
        mock_client.get_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        analysis = {
            "strong_trend": True,
            "current_price": 94.0,
        }
        trading.execute_trade("TEST", analysis)
        mock_client.get_position.assert_called_with("TEST")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.SELL
        assert float(order.qty) == 1


def test_stop_loss_does_not_sell_when_above_threshold():
    """When position is down but less than STOP_LOSS_PCT, do not sell on stop-loss."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        # Entry 100, current 96 -> 4% down; 96 > 95 so no stop-loss
        mock_client.get_position.return_value = MagicMock(
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
    """Analysis dict that satisfies all BUY conditions (for notional/whole-share tests)."""
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
    }


def test_notional_mode_buys_whole_share_when_price_le_notional():
    """When NOTIONAL_PER_TRADE is set and price <= notional and we have buying power, buy 1 whole share."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=100.0):
        mock_client.get_position.side_effect = Exception("position does not exist")
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
        mock_client.get_position.side_effect = Exception("position does not exist")
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
        mock_client.get_position.side_effect = Exception("position does not exist")
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
        mock_client.get_position.side_effect = Exception("position does not exist")
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
        mock_client.get_position.side_effect = Exception("position does not exist")
        mock_client.get_all_positions.return_value = []
        analysis = _buy_conditions_for_notional()
        analysis["current_price"] = 200.0
        trading.execute_trade("TEST", analysis)
        mock_client.submit_order.assert_not_called()


# ─── PDT awareness ───


def test_pdt_blocks_stop_loss_sell():
    """When PDT limit reached, stop-loss SELL is skipped (no order submitted)."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_pdt", return_value=True):
        mock_client.get_position.return_value = MagicMock(
            qty=1,
            avg_entry_price="100.0",
        )
        analysis = {
            "strong_trend": True,
            "current_price": 94.0,  # below stop-loss threshold
        }
        trading.execute_trade("TEST", analysis)
        mock_client.get_position.assert_called_with("TEST")
        mock_client.submit_order.assert_not_called()


def test_pdt_blocks_signal_sell():
    """When PDT limit reached, signal-based SELL is skipped."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_pdt", return_value=True):
        mock_client.get_position.return_value = MagicMock(qty=1, avg_entry_price="100.0")
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
        mock_client.get_position.assert_called_with("TEST")
        mock_client.submit_order.assert_not_called()


def test_pdt_blocks_trailing_stop_sell():
    """When PDT limit reached, trailing-stop SELL is skipped."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_should_block_sell_pdt", return_value=True), \
         patch.object(trading, "_get_trail_running_high", return_value=110.0):
        mock_client.get_position.return_value = MagicMock(
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
        mock_client.submit_order.assert_not_called()


# ─── Trailing stop ───


def test_trailing_stop_sells_when_active_and_price_drops_from_high():
    """When trail is active (price was 5%+ above entry) and price drops 4% from running high, submit SELL."""
    with _patch_trade_limits(), patch.object(trading, "trading_client") as mock_client:
        mock_client.get_position.return_value = MagicMock(
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
        mock_client.get_position.return_value = MagicMock(
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
        mock_client.get_position.return_value = MagicMock(
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
