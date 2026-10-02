"""
Unit tests for backtest.run_walk_forward's window splitting and aggregate
stats. run_backtest itself hits DuckDB, so it's stubbed here — these tests
only verify the walk-forward slicing/math, not the strategy logic.
"""
import pytest

import backtest


def _stub_run_backtest(returns_by_window):
    """Return a fake run_backtest that yields the next canned result per call."""
    calls = []

    def _fake(symbols, start, end, initial_capital=100_000.0, table=None, equity_curve_path=None):
        idx = len(calls)
        calls.append((start, end))
        total_return_pct, max_drawdown_pct, num_trades, win_rate = returns_by_window[idx]
        return {
            "total_return_pct": total_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "num_trades": num_trades,
            "win_rate": win_rate,
            "final_equity": initial_capital * (1 + total_return_pct / 100),
            "trades": [],
        }

    return _fake, calls


def test_walk_forward_non_overlapping_window_count(monkeypatch):
    fake, calls = _stub_run_backtest([(5.0, 2.0, 3, 66.7)] * 2)
    monkeypatch.setattr(backtest, "run_backtest", fake)

    result = backtest.run_walk_forward(
        ["AAPL"], "2024-01-01", "2025-01-01", window_days=180, step_days=180,
    )

    assert result["n_windows"] == 2
    assert len(calls) == 2
    # Windows should be contiguous, non-overlapping
    assert calls[0][1] == calls[1][0]


def test_walk_forward_overlapping_windows_more_samples(monkeypatch):
    fake, calls = _stub_run_backtest([(1.0, 1.0, 1, 100.0)] * 4)
    monkeypatch.setattr(backtest, "run_backtest", fake)

    result = backtest.run_walk_forward(
        ["AAPL"], "2024-01-01", "2025-01-01", window_days=180, step_days=60,
    )

    assert result["n_windows"] == len(calls) == 4


def test_walk_forward_aggregate_stats(monkeypatch):
    # Three windows: +10%, -10%, 0% -> mean 0, two non-profitable, one profitable
    fake, _ = _stub_run_backtest([
        (10.0, 3.0, 2, 50.0),
        (-10.0, 8.0, 2, 0.0),
        (0.0, 1.0, 0, 0.0),
    ])
    monkeypatch.setattr(backtest, "run_backtest", fake)

    result = backtest.run_walk_forward(
        ["AAPL"], "2024-01-01", "2024-10-01", window_days=90, step_days=90,
    )

    assert result["n_windows"] == 3
    assert result["mean_return_pct"] == pytest.approx(0.0)
    assert result["best_return_pct"] == 10.0
    assert result["worst_return_pct"] == -10.0
    assert result["pct_profitable_windows"] == pytest.approx(1 / 3 * 100)
    assert result["worst_drawdown_pct"] == 8.0


def test_walk_forward_raises_when_no_window_fits(monkeypatch):
    fake, _ = _stub_run_backtest([])
    monkeypatch.setattr(backtest, "run_backtest", fake)

    with pytest.raises(ValueError, match="No full"):
        backtest.run_walk_forward(
            ["AAPL"], "2024-01-01", "2024-02-01", window_days=180, step_days=90,
        )
