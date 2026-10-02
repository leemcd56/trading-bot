"""
Tests for ai_review.confirm_trade and its wiring into execute_trade.

confirm_trade is the second-opinion AI gate: the rules engine decides it wants
to BUY/SELL first, and only that decision is handed to the LLM to confirm or
veto. These tests cover the gate itself (disabled-by-default, missing key,
parsed CONFIRM/VETO, fail-closed on error) and that execute_trade actually
calls it before placing a TA-driven order, without re-testing the TA gates
themselves (see test_trading.py for those).
"""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

import ai_review
import trading


def _buy_analysis():
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
        "current_price": 100.0,
    }


def _sell_analysis():
    return {
        "strong_trend": True,
        "near_lower_band": True,
        "sar_above_price": False,
        "sar_flipped_to_bear": False,
        "dive_bombing": False,
        "bearish_crossover": False,
        "current_price": 100.0,
    }


# ─── confirm_trade itself ──────────────────────────────────────────────────


def test_disabled_by_default_confirms_without_network_call():
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", False), \
         patch("ai_review.requests.post") as mock_post:
        verdict = ai_review.confirm_trade("TEST", "BUY", _buy_analysis(), "moderate")
        assert verdict["decision"] == "CONFIRM"
        mock_post.assert_not_called()


def test_enabled_without_api_key_vetoes():
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(ai_review, "ANTHROPIC_API_KEY", ""), \
         patch("ai_review.requests.post") as mock_post:
        verdict = ai_review.confirm_trade("TEST", "BUY", _buy_analysis(), "moderate")
        assert verdict["decision"] == "VETO"
        mock_post.assert_not_called()


def _mock_response(decision: str, reasoning: str = "looks fine"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "content": [
            {"type": "text", "text": f'{{"decision": "{decision}", "reasoning": "{reasoning}"}}'}
        ]
    }
    return resp


def test_enabled_confirms_on_well_formed_confirm_response():
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(ai_review, "ANTHROPIC_API_KEY", "fake-key"), \
         patch("ai_review.requests.post", return_value=_mock_response("CONFIRM")) as mock_post:
        verdict = ai_review.confirm_trade("TEST", "BUY", _buy_analysis(), "moderate")
        assert verdict["decision"] == "CONFIRM"
        mock_post.assert_called_once()


def test_enabled_vetoes_on_well_formed_veto_response():
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(ai_review, "ANTHROPIC_API_KEY", "fake-key"), \
         patch("ai_review.requests.post", return_value=_mock_response("VETO", "indicators conflict")):
        verdict = ai_review.confirm_trade("TEST", "SELL", _sell_analysis(), "moderate")
        assert verdict["decision"] == "VETO"
        assert "conflict" in verdict["reasoning"]


def test_network_error_fails_closed_to_veto():
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(ai_review, "ANTHROPIC_API_KEY", "fake-key"), \
         patch("ai_review.requests.post", side_effect=TimeoutError("boom")):
        verdict = ai_review.confirm_trade("TEST", "BUY", _buy_analysis(), "moderate")
        assert verdict["decision"] == "VETO"


def test_malformed_json_response_fails_closed_to_veto():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"content": [{"type": "text", "text": "not json"}]}
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(ai_review, "ANTHROPIC_API_KEY", "fake-key"), \
         patch("ai_review.requests.post", return_value=resp):
        verdict = ai_review.confirm_trade("TEST", "BUY", _buy_analysis(), "moderate")
        assert verdict["decision"] == "VETO"


def test_markdown_fenced_json_response_still_parses():
    """Models sometimes wrap JSON in a ```json fence despite being told not
    to - confirm_trade should still extract and parse it rather than
    fail-closed veto a legitimate CONFIRM."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    fenced_text = '```json\n{"decision": "CONFIRM", "reasoning": "all clear"}\n```'
    resp.json.return_value = {"content": [{"type": "text", "text": fenced_text}]}
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(ai_review, "ANTHROPIC_API_KEY", "fake-key"), \
         patch("ai_review.requests.post", return_value=resp):
        verdict = ai_review.confirm_trade("TEST", "BUY", _buy_analysis(), "moderate")
        assert verdict["decision"] == "CONFIRM"
        assert verdict["reasoning"] == "all clear"


def test_json_response_with_stray_prose_still_parses():
    """A model that adds a sentence before/after the JSON object should still
    be parsed correctly by extracting the {...} block."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    text = 'Sure, here is my review:\n{"decision": "VETO", "reasoning": "contradictory signals"}\nHope that helps!'
    resp.json.return_value = {"content": [{"type": "text", "text": text}]}
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(ai_review, "ANTHROPIC_API_KEY", "fake-key"), \
         patch("ai_review.requests.post", return_value=resp):
        verdict = ai_review.confirm_trade("TEST", "SELL", _sell_analysis(), "moderate")
        assert verdict["decision"] == "VETO"
        assert verdict["reasoning"] == "contradictory signals"


# ─── wiring into execute_trade ─────────────────────────────────────────────


def _patch_trade_limits():
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
        _should_block_sell_min_hold=lambda symbol: False,
        _last_buy_source=lambda symbol: None,
        MIN_HOLD_HOURS=0,
    )


def test_execute_trade_skips_buy_when_ai_vetoes():
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_get_buying_power", return_value=100_000.0), \
         patch.object(trading, "_get_account_equity", return_value=100_000.0), \
         patch.object(trading, "confirm_trade", return_value={"decision": "VETO", "reasoning": "nope"}) as mock_confirm:
        mock_client.get_all_positions.return_value = []
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_trade("TEST", _buy_analysis())
        mock_confirm.assert_called_once_with("TEST", "BUY", _buy_analysis(), trading.TRADING_MODE)
        mock_client.submit_order.assert_not_called()


def test_execute_trade_returns_none_when_buy_vetoed():
    """A vetoed BUY must return None, not a truthy string - main.py's ta_job()
    treats a truthy return as 'no signal' and would otherwise double-alert
    (the veto already sent its own alert) and mislabel an active veto as
    'no signal'."""
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_get_buying_power", return_value=100_000.0), \
         patch.object(trading, "confirm_trade", return_value={"decision": "VETO", "reasoning": "nope"}):
        mock_client.get_all_positions.return_value = []
        result = trading.execute_trade("TEST", _buy_analysis())
        assert result is None


def test_ai_gate_not_consulted_when_buy_unaffordable():
    """The AI gate is a real network call - it must not be reached for a BUY
    that will be skipped anyway for insufficient buying power."""
    analysis = {**_buy_analysis(), "current_price": 100.0}
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_get_account_equity", return_value=1000.0), \
         patch.object(trading, "_get_buying_power", return_value=0.0), \
         patch.object(trading, "confirm_trade") as mock_confirm:
        mock_client.get_all_positions.return_value = []
        trading.execute_trade("TEST", analysis)
        mock_confirm.assert_not_called()
        mock_client.submit_order.assert_not_called()


def test_execute_trade_places_buy_when_ai_confirms():
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_get_buying_power", return_value=100_000.0), \
         patch.object(trading, "_get_account_equity", return_value=100_000.0), \
         patch.object(trading, "confirm_trade", return_value={"decision": "CONFIRM", "reasoning": "ok"}):
        mock_client.get_all_positions.return_value = []
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_trade("TEST", _buy_analysis())
        mock_client.submit_order.assert_called_once()


def test_execute_trade_skips_sell_when_ai_vetoes():
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "confirm_trade", return_value={"decision": "VETO", "reasoning": "nope"}) as mock_confirm:
        mock_client.get_open_position.return_value = MagicMock(qty=1)
        trading.execute_trade("TEST", _sell_analysis())
        mock_confirm.assert_called_once_with("TEST", "SELL", _sell_analysis(), trading.TRADING_MODE)
        mock_client.submit_order.assert_not_called()


def test_execute_trade_places_sell_when_ai_confirms():
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "confirm_trade", return_value={"decision": "CONFIRM", "reasoning": "ok"}):
        mock_client.get_open_position.return_value = MagicMock(qty=1)
        trading.execute_trade("TEST", _sell_analysis())
        mock_client.submit_order.assert_called_once()


def test_ai_alert_suffix_empty_when_gate_disabled():
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", False):
        assert trading._ai_alert_suffix({"decision": "CONFIRM", "reasoning": "looks good"}) == ""


def test_ai_alert_suffix_includes_reasoning_when_gate_enabled():
    with patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True):
        suffix = trading._ai_alert_suffix({"decision": "CONFIRM", "reasoning": "looks good"})
        assert suffix == " | AI: looks good"


def test_buy_alert_includes_ai_reasoning_when_gate_enabled():
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "_get_buying_power", return_value=100_000.0), \
         patch.object(trading, "_get_account_equity", return_value=100_000.0), \
         patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(trading, "confirm_trade", return_value={"decision": "CONFIRM", "reasoning": "strong uptrend"}), \
         patch.object(trading, "_submit_order", return_value=(True, True)), \
         patch.object(trading, "send_alert") as mock_alert:
        mock_client.get_all_positions.return_value = []
        mock_client.get_open_position.side_effect = Exception("position does not exist")
        trading.execute_trade("TEST", _buy_analysis())
        trade_alert_messages = [call.args[0] for call in mock_alert.call_args_list if call.args[1] == "trade"]
        assert any("strong uptrend" in msg for msg in trade_alert_messages)


def test_sell_alert_includes_ai_reasoning_when_gate_enabled():
    with _patch_trade_limits(), \
         patch.object(trading, "trading_client") as mock_client, \
         patch.object(ai_review, "AI_CONFIRMATION_ENABLED", True), \
         patch.object(trading, "confirm_trade", return_value={"decision": "CONFIRM", "reasoning": "clean breakdown"}), \
         patch.object(trading, "_submit_order", return_value=(True, True)), \
         patch.object(trading, "send_alert") as mock_alert:
        mock_client.get_open_position.return_value = MagicMock(qty=1)
        trading.execute_trade("TEST", _sell_analysis())
        trade_alert_messages = [call.args[0] for call in mock_alert.call_args_list if call.args[1] == "trade"]
        assert any("clean breakdown" in msg for msg in trade_alert_messages)


def test_risk_exit_bypasses_ai_gate():
    """Stop-loss/trailing-stop exits must never go through the AI gate."""
    analysis = {"current_price": 90.0}
    with patch.object(trading, "trading_client") as mock_client, \
         patch.object(trading, "confirm_trade") as mock_confirm, \
         patch.object(trading, "STOP_LOSS_PCT", 0.05):
        mock_client.get_open_position.return_value = MagicMock(qty=1, avg_entry_price=100.0)
        with patch.object(trading, "_submit_order", return_value=(True, True)):
            trading.execute_trade("TEST", analysis)
        mock_confirm.assert_not_called()
