"""
Backfill historical 1-min candle data from Finnhub into DuckDB for backtesting.
Uses a dedicated table (trends_backtest by default) to avoid mixing with live data.
"""
import argparse
import os
import time
import requests
import duckdb
import pandas as pd
from config import DB_PATH
from utils import logger

FINNHUB_BASE = "https://finnhub.io/api/v1/stock/candle"
# Finnhub free tier often limits 1-min to ~1 year; chunk by day to avoid huge responses
CHUNK_SECONDS = 86400  # 1 day
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2

DEFAULT_TABLE = "trends_backtest"


def _fetch_chunk(symbol: str, from_ts: int, to_ts: int, api_key: str) -> pd.DataFrame | None:
    params = {
        "symbol": symbol,
        "resolution": "1",
        "from": from_ts,
        "to": to_ts,
        "token": api_key,
    }
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(FINNHUB_BASE, params=params, timeout=60)
            if r.status_code == 429:
                wait = RETRY_BACKOFF_SEC ** (attempt + 1)
                logger.warning(f"Rate limit for {symbol}, retry in {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(RETRY_BACKOFF_SEC ** (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            break
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Fetch failed for {symbol} [{from_ts}-{to_ts}]: {e}")
                return None
            time.sleep(RETRY_BACKOFF_SEC ** (attempt + 1))

    if not data.get("t") or not data.get("c"):
        return pd.DataFrame()

    return pd.DataFrame({
        "symbol": symbol,
        "timestamp": data["t"],
        "open": data["o"],
        "high": data["h"],
        "low": data["l"],
        "close": data["c"],
        "volume": data.get("v", [0] * len(data["t"])),
    })


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
    api_key: str,
    table: str = DEFAULT_TABLE,
) -> int:
    """Backfill one symbol from start_ts to end_ts (Unix seconds). Returns total bars inserted."""
    con = duckdb.connect(DB_PATH)
    _ensure_table(con, table)
    total = 0
    t = start_ts
    while t < end_ts:
        chunk_end = min(t + CHUNK_SECONDS, end_ts)
        df = _fetch_chunk(symbol, t, chunk_end, api_key)
        if df is None:
            t = chunk_end
            continue
        if len(df) > 0:
            con.register("_chunk", df)
            con.execute(f"""
                DELETE FROM {table}
                WHERE symbol = ? AND timestamp >= ? AND timestamp < ?
            """, [symbol, t, chunk_end])
            con.execute(f"""
                INSERT INTO {table} (symbol, timestamp, open, high, low, close, volume)
                SELECT symbol, timestamp, open, high, low, close, volume FROM _chunk
            """)
            con.unregister("_chunk")
            total += len(df)
        t = chunk_end
        time.sleep(0.2)  # gentle on API
    con.close()
    return total


def main():
    parser = argparse.ArgumentParser(description="Backfill historical candles for backtesting")
    parser.add_argument("--symbols", type=str, default="AAPL,MSFT", help="Comma-separated symbols")
    parser.add_argument("--start", type=str, required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--table", type=str, default=DEFAULT_TABLE, help="DuckDB table name")
    args = parser.parse_args()

    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        logger.error("FINNHUB_API_KEY not set")
        return 1

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
        n = backfill_symbol(symbol, start_ts, end_ts, api_key, args.table)
        logger.info(f"Backfilled {symbol}: {n} bars into {args.table}")

    return 0


if __name__ == "__main__":
    exit(main())
