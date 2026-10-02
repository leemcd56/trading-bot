from unittest.mock import call, patch

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
