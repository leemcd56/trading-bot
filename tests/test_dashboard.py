from fastapi.testclient import TestClient

import dashboard


client = TestClient(dashboard.app)


def test_dashboard_root_shows_paper_not_live_when_healthy(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "get_trading_runtime_status",
        lambda: {
            "orders_allowed": True,
            "state": "paper",
            "label": "Paper",
            "detail": "Paper trading is enabled.",
        },
    )
    monkeypatch.setattr(dashboard, "fetch_account_summary", lambda: {"equity": 1, "cash": 1, "buying_power": 1})
    monkeypatch.setattr(dashboard, "fetch_positions", lambda: [])

    response = client.get("/")

    assert response.status_code == 200
    assert '>Live<' not in response.text
    assert '<div id="statusPill" class="status-pill status-paper"' in response.text
    assert '<span id="statusText">Paper</span>' in response.text


def test_dashboard_root_marks_failed_broker_fetch_as_degraded(monkeypatch):
    monkeypatch.setattr(
        dashboard,
        "get_trading_runtime_status",
        lambda: {
            "orders_allowed": True,
            "state": "paper",
            "label": "Paper",
            "detail": "Paper trading is enabled.",
        },
    )
    monkeypatch.setattr(dashboard, "fetch_account_summary", lambda: None)
    monkeypatch.setattr(dashboard, "fetch_positions", lambda: None)

    response = client.get("/")

    assert response.status_code == 200
    assert '<div id="statusPill" class="status-pill status-degraded"' in response.text
    assert '<span id="lastUpdated">Update failed</span>' in response.text


def test_api_positions_returns_503_on_fetch_failure(monkeypatch):
    monkeypatch.setattr(dashboard, "fetch_positions", lambda: None)

    response = client.get("/api/positions")

    assert response.status_code == 503
    assert response.json() == {"error": "Unable to fetch positions"}
