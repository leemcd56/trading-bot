"""
Tests for signals.fetch_signals — FMP analyst upgrade/downgrade feed.

All HTTP calls are mocked; no network access required.
"""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import requests

import signals


# ─── helpers ─────────────────────────────────────────────────────────────────


def _response(data, *, ok: bool = True):
    """Build a mock requests.Response."""
    mock = MagicMock()
    mock.json.return_value = data
    if ok:
        mock.raise_for_status.return_value = None
    else:
        mock.raise_for_status.side_effect = requests.HTTPError("HTTP error")
    return mock


def _entry(symbol, action, grade, entry_date=None):
    """Build a minimal FMP analyst-action entry."""
    return {
        "date":     entry_date or date.today().isoformat(),
        "symbol":   symbol,
        "action":   action,
        "newGrade": grade,
    }


TODAY = date.today().isoformat()


# ─── short-circuit: no API key ────────────────────────────────────────────────


def test_no_api_key_returns_empty():
    """Without FMP_API_KEY no HTTP call is made and empty lists are returned."""
    with patch.object(signals, "FMP_API_KEY", ""):
        result = signals.fetch_signals()
    assert result == {"buy": [], "sell": []}


# ─── network / response errors ───────────────────────────────────────────────


def test_http_error_returns_empty():
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(None, ok=False)):
        result = signals.fetch_signals()
    assert result == {"buy": [], "sell": []}


def test_connection_exception_returns_empty():
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", side_effect=Exception("timeout")):
        result = signals.fetch_signals()
    assert result == {"buy": [], "sell": []}


def test_non_list_response_returns_empty():
    """FMP returns a dict on API errors (e.g. invalid key); treat as empty."""
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response({"Error Message": "Invalid API KEY"})):
        result = signals.fetch_signals()
    assert result == {"buy": [], "sell": []}


def test_empty_list_response():
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response([])):
        result = signals.fetch_signals()
    assert result == {"buy": [], "sell": []}


# ─── buy signal filtering ────────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["upgrade", "initiated", "reiterated", "maintains"])
def test_buy_actions_accepted(action):
    """All four buy-eligible action types should produce a buy signal."""
    data = [_entry("AAPL", action, "Buy")]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert "AAPL" in result["buy"]


@pytest.mark.parametrize("grade", [
    "Buy", "Strong Buy", "Outperform", "Overweight", "Accumulate",
    "Conviction Buy", "Market Outperform",
])
def test_buy_grades_accepted(grade):
    data = [_entry("AAPL", "upgrade", grade)]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert "AAPL" in result["buy"], f"grade '{grade}' should be a buy signal"


def test_buy_grade_case_insensitive():
    data = [_entry("AAPL", "upgrade", "STRONG BUY")]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert "AAPL" in result["buy"]


# ─── sell signal filtering ───────────────────────────────────────────────────


@pytest.mark.parametrize("action", ["downgrade", "initiated"])
def test_sell_actions_accepted(action):
    data = [_entry("TSLA", action, "Sell")]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert "TSLA" in result["sell"]


@pytest.mark.parametrize("grade", [
    "Sell", "Strong Sell", "Underperform", "Underweight", "Reduce",
    "Market Underperform",
])
def test_sell_grades_accepted(grade):
    data = [_entry("TSLA", "downgrade", grade)]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert "TSLA" in result["sell"], f"grade '{grade}' should be a sell signal"


# ─── exclusion rules ─────────────────────────────────────────────────────────


def test_wrong_date_excluded():
    """Entries from a different date must be filtered out."""
    data = [_entry("AAPL", "upgrade", "Buy", entry_date="2020-01-01")]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert result["buy"] == []
    assert result["sell"] == []


def test_unrecognised_buy_action_excluded():
    """An action type that isn't in the buy whitelist must be ignored."""
    data = [_entry("AAPL", "random_action", "Buy")]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert result["buy"] == []


def test_neutral_grade_excluded():
    """A grade like 'Neutral' or 'Hold' is neither buy nor sell."""
    data = [
        _entry("AAPL", "upgrade", "Neutral"),
        _entry("TSLA", "downgrade", "Hold"),
    ]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert result["buy"] == []
    assert result["sell"] == []


def test_buy_action_with_sell_grade_excluded():
    """upgrade + sell grade shouldn't appear in either list (action/grade mismatch)."""
    data = [_entry("AAPL", "upgrade", "Sell")]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    # upgrade is a buy action; 'Sell' is not in _BUY_GRADES → no signal
    assert "AAPL" not in result["buy"]
    assert "AAPL" not in result["sell"]


# ─── deduplication ───────────────────────────────────────────────────────────


def test_duplicate_symbol_appears_once():
    """Same symbol appearing twice today (two upgrades) → deduplicated to one entry."""
    data = [
        _entry("AAPL", "upgrade",   "Buy"),
        _entry("AAPL", "reiterated", "Strong Buy"),
    ]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert result["buy"].count("AAPL") == 1


def test_symbol_cannot_appear_in_both_lists():
    """A symbol should only land in buy OR sell, not both (first match wins via `seen`)."""
    data = [
        _entry("AAPL", "upgrade",   "Buy"),
        _entry("AAPL", "downgrade", "Sell"),
    ]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    in_buy  = "AAPL" in result["buy"]
    in_sell = "AAPL" in result["sell"]
    assert in_buy != in_sell, "symbol must appear in exactly one list"


# ─── multi-symbol / mixed responses ──────────────────────────────────────────


def test_mixed_buy_and_sell_signals():
    data = [
        _entry("AAPL", "upgrade",   "Outperform"),
        _entry("TSLA", "downgrade", "Underperform"),
        _entry("MSFT", "initiated", "Overweight"),
        _entry("GOOG", "initiated", "Underweight"),
    ]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert sorted(result["buy"])  == ["AAPL", "MSFT"]
    assert sorted(result["sell"]) == ["GOOG", "TSLA"]


# ─── publishedDate fallback ───────────────────────────────────────────────────


def test_published_date_fallback():
    """Some FMP endpoints use 'publishedDate' instead of 'date'; both should work."""
    data = [{
        "publishedDate": f"{TODAY}T10:30:00.000Z",
        "symbol":        "AAPL",
        "action":        "upgrade",
        "newGrade":      "Buy",
    }]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert "AAPL" in result["buy"]


def test_missing_date_fields_excluded():
    """Entry with no 'date' or 'publishedDate' → published=''; date filter drops it."""
    data = [{"symbol": "AAPL", "action": "upgrade", "newGrade": "Buy"}]
    with patch.object(signals, "FMP_API_KEY", "key"), \
         patch("signals.requests.get", return_value=_response(data)):
        result = signals.fetch_signals()
    assert result["buy"] == []
