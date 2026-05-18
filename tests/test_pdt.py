"""
Tests for Pattern Day Trading (PDT) guard logic:
  _count_day_trades_in_last_5_days  — SQL + Python counting logic
  _would_sell_be_day_trade          — today's BUY lookup
  _should_block_sell_pdt            — gate that combines the two

Strategy: use a real temp DuckDB file pre-populated with controlled trade_log
rows, then patch trading.DB_PATH to point at it. This tests the actual SQL
queries and qty-matching logic rather than mocking them away.
"""
import os
import tempfile
import time

import duckdb
import pytest
from unittest.mock import patch

import trading

TRADE_LOG_TABLE = trading.TRADE_LOG_TABLE


# ─── helpers ─────────────────────────────────────────────────────────────────


def _day_start(days_ago: int = 0) -> float:
    """Return the UTC midnight timestamp for today minus `days_ago` days."""
    today = (int(time.time()) // 86400) * 86400
    return float(today - days_ago * 86400)


def _make_db(rows: list) -> str:
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


@pytest.fixture
def empty_db():
    path = _make_db([])
    yield path
    os.unlink(path)


# ─── _count_day_trades_in_last_5_days ────────────────────────────────────────


def test_count_day_trades_empty_log(empty_db):
    with patch.object(trading, "DB_PATH", empty_db):
        assert trading._count_day_trades_in_last_5_days() == 0


def test_count_day_trades_buy_only_no_trade():
    """BUY with no same-day SELL → 0 day trades."""
    path = _make_db([(_day_start() + 3600, "AAPL", "BUY", 1)])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 0
    finally:
        os.unlink(path)


def test_count_day_trades_buy_then_sell_same_day():
    """BUY then SELL same symbol same UTC day → 1 day trade."""
    today = _day_start()
    path = _make_db([
        (today + 3600, "AAPL", "BUY",  1),
        (today + 7200, "AAPL", "SELL", 1),
    ])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 1
    finally:
        os.unlink(path)


def test_count_day_trades_buy_today_sell_yesterday_not_a_day_trade():
    """BUY yesterday, SELL today → different day_ids → 0 day trades."""
    path = _make_db([
        (_day_start(1) + 3600, "AAPL", "BUY",  1),   # yesterday
        (_day_start()  + 3600, "AAPL", "SELL", 1),   # today
    ])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 0
    finally:
        os.unlink(path)


def test_count_day_trades_sell_without_prior_buy():
    """SELL with no matching same-day BUY → 0 day trades."""
    path = _make_db([(_day_start() + 3600, "AAPL", "SELL", 1)])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 0
    finally:
        os.unlink(path)


def test_count_day_trades_two_symbols_same_day():
    """Two buy-sell pairs on the same day, different symbols → 2 day trades."""
    today = _day_start()
    path = _make_db([
        (today + 1000, "AAPL", "BUY",  1),
        (today + 2000, "AAPL", "SELL", 1),
        (today + 3000, "TSLA", "BUY",  2),
        (today + 4000, "TSLA", "SELL", 2),
    ])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 2
    finally:
        os.unlink(path)


def test_count_day_trades_older_than_5_days_excluded():
    """Trades from 6 days ago fall outside the rolling window and must not count."""
    path = _make_db([
        # 6-day-old day trade — should be excluded
        (_day_start(6) + 1000, "AAPL", "BUY",  1),
        (_day_start(6) + 2000, "AAPL", "SELL", 1),
        # Today's day trade — should count
        (_day_start() + 1000, "TSLA", "BUY",  1),
        (_day_start() + 2000, "TSLA", "SELL", 1),
    ])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 1
    finally:
        os.unlink(path)


def test_count_day_trades_partial_close_counts_once():
    """BUY 3 shares, SELL 1 share same day → 1 day trade (partial close, not 3)."""
    today = _day_start()
    path = _make_db([
        (today + 1000, "AAPL", "BUY",  3),
        (today + 2000, "AAPL", "SELL", 1),
    ])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 1
    finally:
        os.unlink(path)


def test_count_day_trades_multiple_sells_each_count():
    """BUY 2 shares, SELL 1 + SELL 1 same day → 2 day trades."""
    today = _day_start()
    path = _make_db([
        (today + 1000, "AAPL", "BUY",  2),
        (today + 2000, "AAPL", "SELL", 1),
        (today + 3000, "AAPL", "SELL", 1),
    ])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 2
    finally:
        os.unlink(path)


def test_count_day_trades_spread_across_several_days():
    """3 day trades spread across past 4 days (within 5-day window) → 3."""
    path = _make_db([
        (_day_start(1) + 1000, "AAPL", "BUY",  1),
        (_day_start(1) + 2000, "AAPL", "SELL", 1),
        (_day_start(2) + 1000, "TSLA", "BUY",  1),
        (_day_start(2) + 2000, "TSLA", "SELL", 1),
        (_day_start(4) + 1000, "GOOG", "BUY",  1),
        (_day_start(4) + 2000, "GOOG", "SELL", 1),
    ])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._count_day_trades_in_last_5_days() == 3
    finally:
        os.unlink(path)


# ─── _would_sell_be_day_trade ─────────────────────────────────────────────────


def test_would_sell_be_day_trade_no_buy_today():
    """No BUY for this symbol today → False."""
    path = _make_db([(_day_start(1) + 3600, "AAPL", "BUY", 1)])   # yesterday
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._would_sell_be_day_trade("AAPL") is False
    finally:
        os.unlink(path)


def test_would_sell_be_day_trade_buy_exists_today():
    """BUY for this symbol today → True."""
    path = _make_db([(_day_start() + 3600, "AAPL", "BUY", 1)])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._would_sell_be_day_trade("AAPL") is True
    finally:
        os.unlink(path)


def test_would_sell_be_day_trade_different_symbol():
    """BUY for TSLA today → AAPL sell would not be a day trade."""
    path = _make_db([(_day_start() + 3600, "TSLA", "BUY", 1)])
    try:
        with patch.object(trading, "DB_PATH", path):
            assert trading._would_sell_be_day_trade("AAPL") is False
    finally:
        os.unlink(path)


def test_would_sell_be_day_trade_empty_log(empty_db):
    with patch.object(trading, "DB_PATH", empty_db):
        assert trading._would_sell_be_day_trade("AAPL") is False


# ─── _should_block_sell_pdt ───────────────────────────────────────────────────


def test_should_block_sell_pdt_no_buy_today_never_blocks(empty_db):
    """No BUY today means it's not a day trade — never blocked regardless of count."""
    with patch.object(trading, "DB_PATH", empty_db), \
         patch.object(trading, "MAX_DAY_TRADES_IN_5_DAYS", 3):
        assert trading._should_block_sell_pdt("AAPL") is False


def test_should_block_sell_pdt_under_limit():
    """BUY today, 1 prior day trade in window (limit=3) → not blocked."""
    path = _make_db([
        # 1 day trade yesterday
        (_day_start(1) + 1000, "TSLA", "BUY",  1),
        (_day_start(1) + 2000, "TSLA", "SELL", 1),
        # New BUY today (would become day trade if sold)
        (_day_start()  + 3600, "AAPL", "BUY",  1),
    ])
    try:
        with patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "MAX_DAY_TRADES_IN_5_DAYS", 3):
            assert trading._should_block_sell_pdt("AAPL") is False
    finally:
        os.unlink(path)


def test_should_block_sell_pdt_at_limit():
    """BUY today and already at the PDT limit (3 day trades) → blocked."""
    path = _make_db([
        # 3 day trades on separate past days
        (_day_start(1) + 1000, "AAPL", "BUY",  1),
        (_day_start(1) + 2000, "AAPL", "SELL", 1),
        (_day_start(2) + 1000, "TSLA", "BUY",  1),
        (_day_start(2) + 2000, "TSLA", "SELL", 1),
        (_day_start(3) + 1000, "GOOG", "BUY",  1),
        (_day_start(3) + 2000, "GOOG", "SELL", 1),
        # New BUY today for MSFT
        (_day_start()  + 3600, "MSFT", "BUY",  1),
    ])
    try:
        with patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "MAX_DAY_TRADES_IN_5_DAYS", 3):
            assert trading._should_block_sell_pdt("MSFT") is True
    finally:
        os.unlink(path)


def test_should_block_sell_pdt_disabled_when_none():
    """MAX_DAY_TRADES_IN_5_DAYS=None → PDT guard is disabled."""
    path = _make_db([(_day_start() + 3600, "AAPL", "BUY", 1)])
    try:
        with patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "MAX_DAY_TRADES_IN_5_DAYS", None):
            assert trading._should_block_sell_pdt("AAPL") is False
    finally:
        os.unlink(path)


def test_should_block_sell_pdt_disabled_when_negative():
    """MAX_DAY_TRADES_IN_5_DAYS=-1 → PDT guard is disabled."""
    path = _make_db([(_day_start() + 3600, "AAPL", "BUY", 1)])
    try:
        with patch.object(trading, "DB_PATH", path), \
             patch.object(trading, "MAX_DAY_TRADES_IN_5_DAYS", -1):
            assert trading._should_block_sell_pdt("AAPL") is False
    finally:
        os.unlink(path)
