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
