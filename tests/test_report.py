"""
Tests for report.fetch_benchmark_comparison: the since-inception buy-and-hold
comparison. The honest check on whether active trading is earning its
complexity over the simplest possible alternative.
"""
import os
import tempfile
import time
from unittest.mock import patch

import duckdb
import pandas as pd
import pytest

os.environ.setdefault("ALPACA_API_KEY", "test-key")
os.environ.setdefault("ALPACA_SECRET_KEY", "test-secret")

import report


def _make_snapshots_db(rows: list[tuple]) -> str:
    """Temp DuckDB file with a portfolio_snapshots table seeded with rows."""
    db_file = tempfile.NamedTemporaryFile(suffix=".duckdb", delete=False)
    db_file.close()
    os.unlink(db_file.name)
    con = duckdb.connect(db_file.name)
    try:
        con.execute("""
            CREATE TABLE portfolio_snapshots (
                timestamp_utc DOUBLE, date_et VARCHAR, label VARCHAR, equity DOUBLE
            )
        """)
        if rows:
            con.executemany("INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?)", rows)
    finally:
        con.close()
    return db_file.name


def _candles(prices: list[float], start_ts: float, step_seconds: float = 86400) -> pd.DataFrame:
    return pd.DataFrame({
        "symbol": ["SPY"] * len(prices),
        "timestamp": [start_ts + i * step_seconds for i in range(len(prices))],
        "open": prices,
        "high": prices,
        "low": prices,
        "close": prices,
        "volume": [1000.0] * len(prices),
    })


def test_first_portfolio_snapshot_none_when_empty():
    db_path = _make_snapshots_db([])
    try:
        with patch.object(report, "DB_PATH", db_path):
            assert report._first_portfolio_snapshot() is None
    finally:
        os.unlink(db_path)


def test_first_portfolio_snapshot_returns_earliest_open_row():
    db_path = _make_snapshots_db([
        (200.0, "2026-01-02", "open", 10500.0),
        (100.0, "2026-01-01", "open", 10000.0),  # earliest
        (150.0, "2026-01-01", "close", 10200.0),  # wrong label, must be ignored
    ])
    try:
        with patch.object(report, "DB_PATH", db_path):
            assert report._first_portfolio_snapshot() == (100.0, "2026-01-01", 10000.0)
    finally:
        os.unlink(db_path)


def test_fetch_benchmark_comparison_none_without_inception_snapshot():
    db_path = _make_snapshots_db([])
    try:
        with patch.object(report, "DB_PATH", db_path):
            assert report.fetch_benchmark_comparison() is None
    finally:
        os.unlink(db_path)


def test_fetch_benchmark_comparison_none_when_account_unavailable():
    inception_ts = time.time() - 10 * 86400
    db_path = _make_snapshots_db([(inception_ts, "2026-01-01", "open", 10000.0)])
    try:
        with patch.object(report, "DB_PATH", db_path), \
             patch.object(report, "fetch_account_summary", return_value=None):
            assert report.fetch_benchmark_comparison() is None
    finally:
        os.unlink(db_path)


def test_fetch_benchmark_comparison_none_when_candles_unavailable():
    inception_ts = time.time() - 10 * 86400
    db_path = _make_snapshots_db([(inception_ts, "2026-01-01", "open", 10000.0)])
    try:
        with patch.object(report, "DB_PATH", db_path), \
             patch.object(report, "fetch_account_summary", return_value={"equity": 11000.0, "cash": 0, "buying_power": 0}), \
             patch.object(report, "get_daily_candles_with_failover", return_value=pd.DataFrame()):
            assert report.fetch_benchmark_comparison() is None
    finally:
        os.unlink(db_path)


def test_fetch_benchmark_comparison_none_when_no_candle_on_or_after_inception():
    """All fetched candles predate the inception snapshot -> nothing to compare against."""
    inception_ts = time.time() - 10 * 86400
    db_path = _make_snapshots_db([(inception_ts, "2026-01-01", "open", 10000.0)])
    try:
        stale_candles = _candles([400.0, 405.0], start_ts=inception_ts - 30 * 86400)
        with patch.object(report, "DB_PATH", db_path), \
             patch.object(report, "fetch_account_summary", return_value={"equity": 11000.0, "cash": 0, "buying_power": 0}), \
             patch.object(report, "get_daily_candles_with_failover", return_value=stale_candles):
            assert report.fetch_benchmark_comparison() is None
    finally:
        os.unlink(db_path)


def test_fetch_benchmark_comparison_computes_bot_and_benchmark_returns():
    inception_ts = time.time() - 10 * 86400
    db_path = _make_snapshots_db([(inception_ts, "2026-01-01", "open", 10000.0)])
    try:
        # Benchmark starts at 400 (on inception day) -> currently 440 = +10%
        candles = _candles([400.0, 410.0, 420.0, 440.0], start_ts=inception_ts)
        with patch.object(report, "DB_PATH", db_path), \
             patch.object(report, "fetch_account_summary", return_value={"equity": 10500.0, "cash": 0, "buying_power": 0}), \
             patch.object(report, "get_daily_candles_with_failover", return_value=candles), \
             patch.object(report, "get_intraday_price", return_value=None):
            result = report.fetch_benchmark_comparison()
        assert result is not None
        assert result["benchmark_symbol"] == "SPY"
        assert result["bot_return_pct"] == pytest.approx(5.0)       # 10000 -> 10500
        assert result["benchmark_return_pct"] == pytest.approx(10.0)  # 400 -> 440
        assert result["alpha_pct"] == pytest.approx(-5.0)
    finally:
        os.unlink(db_path)


def test_fetch_benchmark_comparison_prefers_live_intraday_price():
    inception_ts = time.time() - 10 * 86400
    db_path = _make_snapshots_db([(inception_ts, "2026-01-01", "open", 10000.0)])
    try:
        candles = _candles([400.0, 410.0], start_ts=inception_ts)
        with patch.object(report, "DB_PATH", db_path), \
             patch.object(report, "fetch_account_summary", return_value={"equity": 10000.0, "cash": 0, "buying_power": 0}), \
             patch.object(report, "get_daily_candles_with_failover", return_value=candles), \
             patch.object(report, "get_intraday_price", return_value=420.0):
            result = report.fetch_benchmark_comparison()
        # Live price (420) used over the last daily close (410) -> +5% not +2.5%
        assert result["benchmark_return_pct"] == pytest.approx(5.0)
    finally:
        os.unlink(db_path)


def test_fetch_benchmark_comparison_respects_custom_symbol_argument():
    inception_ts = time.time() - 5 * 86400
    db_path = _make_snapshots_db([(inception_ts, "2026-01-01", "open", 10000.0)])
    try:
        candles = _candles([100.0, 110.0], start_ts=inception_ts)
        with patch.object(report, "DB_PATH", db_path), \
             patch.object(report, "fetch_account_summary", return_value={"equity": 10000.0, "cash": 0, "buying_power": 0}), \
             patch.object(report, "get_daily_candles_with_failover", return_value=candles) as mock_candles, \
             patch.object(report, "get_intraday_price", return_value=None):
            result = report.fetch_benchmark_comparison("qqq")
        assert result["benchmark_symbol"] == "QQQ"
        mock_candles.assert_called_once()
        assert mock_candles.call_args[0][0] == "QQQ"
    finally:
        os.unlink(db_path)
