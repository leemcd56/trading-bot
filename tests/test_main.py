from unittest.mock import call, patch

import pytest

import main


def test_ta_job_covers_held_symbols_outside_watchlist():
    """Open positions from signal buys must still go through the TA stop path."""
    analysis = {"strong_trend": False, "current_price": 100.0}
    with patch.object(main, "SYMBOLS", ["AAPL"]), \
         patch.object(main, "get_open_position_symbols", return_value={"MSFT"}), \
         patch.object(main, "is_market_open", return_value=True), \
         patch.object(main, "reconcile_pending_orders"), \
         patch.object(main, "fetch_and_store") as fetch_and_store, \
         patch.object(main, "analyze_trends", return_value=analysis) as analyze_trends, \
         patch.object(main, "execute_trade") as execute_trade, \
         patch.object(main, "prune_old_trends"), \
         patch.object(main, "prune_old_trade_log"), \
         patch.object(main, "send_alert"):
        main.ta_job()

    assert fetch_and_store.call_args_list == [call("AAPL"), call("MSFT")]
    assert analyze_trends.call_args_list == [call("AAPL"), call("MSFT")]
    assert execute_trade.call_args_list == [call("AAPL", analysis), call("MSFT", analysis)]


def test_ta_symbols_falls_back_to_local_holdings_when_positions_read_fails():
    """A failed positions read must not collapse known non-watch holdings to an empty set."""
    with patch.object(main, "SYMBOLS", ["AAPL"]), \
         patch.object(main, "_LAST_KNOWN_HELD_SYMBOLS", set()), \
         patch.object(main, "get_open_position_symbols", return_value=None), \
         patch.object(main, "get_locally_known_held_symbols", return_value={"MSFT"}):
        assert main._ta_symbols() == ["AAPL", "MSFT"]


def test_ta_symbols_reuses_last_known_holdings_when_live_and_local_reads_fail():
    """The last successful held-symbol snapshot still protects stops through a later read failure."""
    with patch.object(main, "SYMBOLS", ["AAPL"]), \
         patch.object(main, "_LAST_KNOWN_HELD_SYMBOLS", {"MSFT"}), \
         patch.object(main, "get_open_position_symbols", return_value=None), \
         patch.object(main, "get_locally_known_held_symbols", return_value=None):
        assert main._ta_symbols() == ["AAPL", "MSFT"]


def test_ta_job_sends_heartbeat_on_full_cycle():
    """A completed cycle must ping the dead-man's-switch exactly once."""
    with patch.object(main, "is_market_open", return_value=True), \
         patch.object(main, "reconcile_pending_orders"), \
         patch.object(main, "_ta_symbols", return_value=[]), \
         patch.object(main, "prune_old_trends"), \
         patch.object(main, "prune_old_trade_log"), \
         patch.object(main, "send_heartbeat") as send_heartbeat:
        main.ta_job()
    send_heartbeat.assert_called_once()


def test_ta_job_sends_heartbeat_when_market_closed():
    """
    The heartbeat must still fire on the early-return path - it proves the
    process/scheduler is alive, independent of whether the market is open.
    """
    with patch.object(main, "is_market_open", return_value=False), \
         patch.object(main, "send_heartbeat") as send_heartbeat:
        main.ta_job()
    send_heartbeat.assert_called_once()


def test_ta_job_sends_heartbeat_even_if_reconcile_raises():
    """
    A finally-block heartbeat must still fire if something above it raises -
    the process is still alive and ticking, even though this cycle errored.
    Only a true hang (never reaching the finally) should suppress the ping.
    """
    with patch.object(main, "is_market_open", return_value=True), \
         patch.object(main, "reconcile_pending_orders", side_effect=RuntimeError("boom")), \
         patch.object(main, "send_heartbeat") as send_heartbeat:
        with pytest.raises(RuntimeError):
            main.ta_job()
    send_heartbeat.assert_called_once()
