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
    with patch.object(trading, "trading_client") as mock_client:
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
    with patch.object(trading, "trading_client") as mock_client:
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
    with patch.object(trading, "trading_client") as mock_client:
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
    with patch.object(trading, "trading_client") as mock_client:
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
        # submit_order may still have been called once for the sell attempt before we check;
        # the actual code calls get_position then submit_order. So submit_order is called with the sell.
        # Actually re-reading the code: get_position is inside try, and if it raises "position does not exist"
        # we don't call submit_order. So submit_order should not be called.
        mock_client.submit_order.assert_not_called()
