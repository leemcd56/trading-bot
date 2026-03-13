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
    """Avoid touching real DB in tests: mock trade log to allow trades."""
    return patch.multiple(
        trading,
        _count_daily=lambda: 0,
        _count_weekly=lambda: 0,
        _record_trade=lambda symbol, side: None,
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
