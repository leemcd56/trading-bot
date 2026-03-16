"""
Backfill historical daily candle data into DuckDB for backtesting.
Uses unified providers (Massive → Yahoo Finance → Finnhub) and
stores into a dedicated table (trends_backtest by default).
"""
import argparse
import time
import duckdb
import pandas as pd
import os
from config import DB_PATH
from utils import logger
from data_providers import get_daily_candles_with_failover

DEFAULT_TABLE = "trends_backtest"


def _ensure_table(con: duckdb.DuckDBPyConnection, table: str) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {table} (
            symbol VARCHAR,
            timestamp BIGINT,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE
        )
    """)


def backfill_symbol(
    symbol: str,
    start_ts: int,
    end_ts: int,
    table: str = DEFAULT_TABLE,
) -> int:
    """Backfill one symbol from start_ts to end_ts (Unix seconds). Returns total bars inserted."""
    con = duckdb.connect(DB_PATH)
    _ensure_table(con, table)
    # We use the same provider failover as live, but restricted to the requested window.
    df_all = get_daily_candles_with_failover(symbol, lookback_days=int((end_ts - start_ts) / 86400) + 1)
    if df_all is None or len(df_all) == 0:
        logger.warning(f"No backfill data for {symbol} in requested range")
        con.close()
        return 0

    df = df_all[(df_all["timestamp"] >= start_ts) & (df_all["timestamp"] < end_ts)]
    if len(df) > 0:
        con.register("_chunk", df)
        con.execute(
            f"""
            DELETE FROM {table}
            WHERE symbol = ? AND timestamp >= ? AND timestamp < ?
            """,
            [symbol, start_ts, end_ts],
        )
        con.execute(
            f"""
            INSERT INTO {table} (symbol, timestamp, open, high, low, close, volume)
            SELECT symbol, timestamp, open, high, low, close, volume FROM _chunk
            """
        )
        con.unregister("_chunk")
        total = len(df)
    else:
        total = 0
    con.close()
    return total


def main():
    parser = argparse.ArgumentParser(description="Backfill historical candles for backtesting")
    parser.add_argument("--symbols", type=str, default="AAPL,MSFT", help="Comma-separated symbols")
    parser.add_argument("--start", type=str, required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--table", type=str, default=DEFAULT_TABLE, help="DuckDB table name")
    args = parser.parse_args()

    try:
        start_dt = time.strptime(args.start, "%Y-%m-%d")
        end_dt = time.strptime(args.end, "%Y-%m-%d")
    except ValueError as e:
        logger.error(f"Invalid date format: {e}")
        return 1

    start_ts = int(time.mktime(start_dt))
    end_ts = int(time.mktime(end_dt)) + 86400  # end of end day

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    for symbol in symbols:
        n = backfill_symbol(symbol, start_ts, end_ts, args.table)
        logger.info(f"Backfilled {symbol}: {n} bars into {args.table}")

    return 0


if __name__ == "__main__":
    exit(main())
