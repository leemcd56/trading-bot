"""
Tests for execute_signal_buy and execute_signal_sell.

execute_signal_buy  — external oracle buy: respects daily/weekly/position caps,
                      double-buy guard, notional sizing, qty fallback.
execute_signal_sell — external oracle sell: 24-hour hold guard, PDT guard,
                      position check, SELL submission.

The 24-hour hold check reads trade_log from the DB directly, so those tests
use a real temp DuckDB file (same pattern as test_pdt.py).  All other DB
helpers (_record_trade, _record_trade_history, etc.) are mocked.
"""
import os
import tempfile
import time

import duckdb
import pytest
from unittest.mock import MagicMock, patch

import trading
from alpaca.trading.enums import OrderSide

TRADE_LOG_TABLE = trading.TRADE_LOG_TABLE


# ─── helpers ─────────────────────────────────────────────────────────────────


def _make_trade_log_db(rows: list) -> str:
    """
    Create a temp DuckDB file with trade_log pre-populated from `rows`.
    Each row is (timestamp_utc, symbol, side, qty).
    Returns the file path — caller is responsible for os.unlink().
    """
    f = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    f.close()
    os.unlink(f.name)   # DuckDB refuses to open an existing non-database file
    con = duckdb.connect(f.name)
    con.execute(f"""
        CREATE TABLE {TRADE_LOG_TABLE} (
            timestamp_utc DOUBLE,
            symbol        VARCHAR,
            side          VARCHAR,
            qty           DOUBLE
        )
    """)
    for row in rows:
        con.execute(
            f"INSERT INTO {TRADE_LOG_TABLE} VALUES (?, ?, ?, ?)", list(row)
        )
    con.close()
    return f.name


def _patch_db_helpers():
    """
    Suppress all DB-writing helpers so no test touches MotherDuck.
    Does NOT patch _should_block_sell_pdt or the direct duckdb.connect calls
    inside execute_signal_sell (those are controlled per-test).
    """
    return patch.multiple(
        trading,
        _record_trade=lambda symbol, side, qty=0: None,
        _record_trade_history=lambda *a, **kw: None,
        _clear_trail_state=lambda symbol: None,
    )


# ─── execute_signal_buy ───────────────────────────────────────────────────────


def test_signal_buy_daily_cap_blocks():
    """Daily cap reached → no order submitted."""
    with _patch_db_helpers(), \
         patch.object(trading, "_count_daily", return_value=3), \
         patch.object(trading, "MAX_DAILY_TRADES", 3), \
         patch.object(trading, "trading_client") as mock_client:
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_not_called()


def test_signal_buy_weekly_cap_blocks():
    """Weekly cap reached → no order submitted."""
    with _patch_db_helpers(), \
         patch.object(trading, "_count_daily", return_value=0), \
         patch.object(trading, "_count_weekly", return_value=8), \
         patch.object(trading, "MAX_DAILY_TRADES", 99), \
         patch.object(trading, "MAX_WEEKLY_TRADES", 8), \
         patch.object(trading, "trading_client") as mock_client:
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_not_called()


def test_signal_buy_max_positions_blocks():
    """Max open positions reached → no order submitted."""
    with _patch_db_helpers(), \
         patch.object(trading, "_count_daily", return_value=0), \
         patch.object(trading, "_count_weekly", return_value=0), \
         patch.object(trading, "_open_positions_count", return_value=4), \
         patch.object(trading, "MAX_DAILY_TRADES", 99), \
         patch.object(trading, "MAX_WEEKLY_TRADES", 99), \
         patch.object(trading, "MAX_OPEN_POSITIONS", 4), \
         patch.object(trading, "trading_client") as mock_client:
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_not_called()


def test_signal_buy_already_holding_skips():
    """Already holding a non-zero position → no order submitted."""
    with _patch_db_helpers(), \
         patch.object(trading, "_count_daily", return_value=0), \
         patch.object(trading, "_count_weekly", return_value=0), \
         patch.object(trading, "_open_positions_count", return_value=0), \
         patch.object(trading, "MAX_DAILY_TRADES", 99), \
         patch.object(trading, "MAX_WEEKLY_TRADES", 99), \
         patch.object(trading, "MAX_OPEN_POSITIONS", 99), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.return_value = MagicMock(qty=3)
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_not_called()


def _caps_ok():
    """Return patches that clear all cap checks."""
    return patch.multiple(
        trading,
        _count_daily=lambda: 0,
        _count_weekly=lambda: 0,
        _open_positions_count=lambda: 0,
        MAX_DAILY_TRADES=99,
        MAX_WEEKLY_TRADES=99,
        MAX_OPEN_POSITIONS=99,
    )


def test_signal_buy_notional_fractional_when_price_above_notional():
    """Price > notional → fractional buy at exactly NOTIONAL_PER_TRADE dollars."""
    with _patch_db_helpers(), _caps_ok(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=500.0), \
         patch("trading.get_intraday_price", return_value=300.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "notional", None) == 75.0
        assert getattr(order, "qty", None) is None


def test_signal_buy_notional_whole_share_when_price_le_notional():
    """Price <= notional and buying_power >= price → buy 1 whole share."""
    with _patch_db_helpers(), _caps_ok(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=500.0), \
         patch("trading.get_intraday_price", return_value=50.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "qty", None) == 1
        assert getattr(order, "notional", None) is None


def test_signal_buy_notional_capped_by_buying_power():
    """Buying power < NOTIONAL_PER_TRADE → notional is capped to buying power."""
    with _patch_db_helpers(), _caps_ok(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=30.0), \
         patch("trading.get_intraday_price", return_value=300.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "notional", None) == 30.0


def test_signal_buy_skips_when_buying_power_below_minimum():
    """Effective notional < $1 (Alpaca minimum) → no order submitted."""
    with _patch_db_helpers(), _caps_ok(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=0.50), \
         patch("trading.get_intraday_price", return_value=300.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_not_called()


def test_signal_buy_qty_mode_buys_one_share():
    """NOTIONAL_PER_TRADE=None → qty-mode; always buy exactly 1 share."""
    with _patch_db_helpers(), _caps_ok(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", None), \
         patch("trading.get_intraday_price", return_value=150.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_signal_buy("AAPL")
        mock_client.submit_order.assert_called_once()
        order = mock_client.submit_order.call_args[0][0]
        assert order.side == OrderSide.BUY
        assert getattr(order, "qty", None) == 1


def test_signal_buy_order_failure_does_not_raise():
    """If submit_order raises, execute_signal_buy must not propagate the exception."""
    with _patch_db_helpers(), _caps_ok(), \
         patch.object(trading, "NOTIONAL_PER_TRADE", 75), \
         patch.object(trading, "_get_buying_power", return_value=500.0), \
         patch("trading.get_intraday_price", return_value=300.0), \
         patch.object(trading, "trading_client") as mock_client:
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        mock_client.submit_order.side_effect = Exception("broker error")
        # Should not raise
        trading.execute_signal_buy("AAPL")


# ─── execute_signal_sell ─────────────────────────────────────────────────────


def test_signal_sell_no_buy_record_skips():
    """No BUY in trade_log for this symbol → skip without submitting."""
    path = _make_trade_log_db([])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=False), \
             patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "trading_client") as mock_client:
            trading.execute_signal_sell("AAPL")
            mock_client.submit_order.assert_not_called()
    finally:
        os.unlink(path)


def test_signal_sell_held_less_than_24h_skips():
    """Position bought 1 hour ago (< 24h hold) → skip to avoid unintended day trade."""
    path = _make_trade_log_db([(time.time() - 3600, "AAPL", "BUY", 1)])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=False), \
             patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "trading_client") as mock_client:
            trading.execute_signal_sell("AAPL")
            mock_client.submit_order.assert_not_called()
    finally:
        os.unlink(path)


def test_signal_sell_held_exactly_24h_is_allowed():
    """
    Position bought 24h + 1 second ago → satisfies the hold requirement.
    (Edge: held_seconds >= 86400 must pass.)
    """
    path = _make_trade_log_db([(time.time() - 86401, "AAPL", "BUY", 1)])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=False), \
             patch.object(trading, "DB_PATH", path), \
             patch("trading.get_intraday_price", return_value=150.0), \
             patch.object(trading, "trading_client") as mock_client:
            mock_client.get_open_position.return_value = MagicMock(qty=2)
            trading.execute_signal_sell("AAPL")
            mock_client.submit_order.assert_called_once()
            assert mock_client.submit_order.call_args[0][0].side == OrderSide.SELL
    finally:
        os.unlink(path)


def test_signal_sell_pdt_block_skips():
    """PDT limit reached → skip SELL even if hold time is satisfied."""
    path = _make_trade_log_db([(time.time() - 90000, "AAPL", "BUY", 1)])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=True), \
             patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "trading_client") as mock_client:
            trading.execute_signal_sell("AAPL")
            mock_client.submit_order.assert_not_called()
    finally:
        os.unlink(path)


def test_signal_sell_no_open_position_skips():
    """Hold time and PDT OK but no open position → skip."""
    path = _make_trade_log_db([(time.time() - 90000, "AAPL", "BUY", 1)])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=False), \
             patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "trading_client") as mock_client:
            mock_client.get_open_position.side_effect = Exception("position does not exist")
            trading.execute_signal_sell("AAPL")
            mock_client.submit_order.assert_not_called()
    finally:
        os.unlink(path)


def test_signal_sell_position_qty_zero_skips():
    """Position exists but qty=0 → skip."""
    path = _make_trade_log_db([(time.time() - 90000, "AAPL", "BUY", 1)])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=False), \
             patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "trading_client") as mock_client:
            mock_client.get_open_position.return_value = MagicMock(qty=0)
            trading.execute_signal_sell("AAPL")
            mock_client.submit_order.assert_not_called()
    finally:
        os.unlink(path)


def test_signal_sell_submits_correct_qty():
    """All checks pass → SELL submitted with the full position qty."""
    path = _make_trade_log_db([(time.time() - 90000, "AAPL", "BUY", 1)])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=False), \
             patch.object(trading, "DB_PATH", path), \
             patch("trading.get_intraday_price", return_value=150.0), \
             patch.object(trading, "trading_client") as mock_client:
            mock_client.get_open_position.return_value = MagicMock(qty=5)
            trading.execute_signal_sell("AAPL")
            mock_client.submit_order.assert_called_once()
            order = mock_client.submit_order.call_args[0][0]
            assert order.side == OrderSide.SELL
            assert float(order.qty) == 5.0
    finally:
        os.unlink(path)


def test_signal_sell_order_failure_does_not_raise():
    """If submit_order raises, execute_signal_sell must not propagate the exception."""
    path = _make_trade_log_db([(time.time() - 90000, "AAPL", "BUY", 1)])
    try:
        with _patch_db_helpers(), \
             patch.object(trading, "_should_block_sell_pdt", return_value=False), \
             patch.object(trading, "DB_PATH", path), \
             patch("trading.get_intraday_price", return_value=150.0), \
             patch.object(trading, "trading_client") as mock_client:
            mock_client.get_open_position.return_value = MagicMock(qty=1)
            mock_client.submit_order.side_effect = Exception("broker error")
            # Should not raise
            trading.execute_signal_sell("AAPL")
    finally:
        os.unlink(path)
