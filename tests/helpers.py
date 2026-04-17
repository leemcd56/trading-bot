"""
Helpers for tests: build spoofed OHLC data and load it into a DuckDB connection.
Use 1-min bars; analysis needs at least 50 bars and uses up to 300.
"""
import time
import duckdb
import pandas as pd
import numpy as np

# ~300 1-min bars (analysis LIMIT 300); need 50+ for indicators
NUM_BARS = 300
# Anchor timestamps to now so the staleness check (7-day window) always passes
BASE_TS = int(time.time()) - NUM_BARS * 60


def make_ohlc(
    symbol: str,
    start_price: float,
    end_price: float,
    num_bars: int = NUM_BARS,
    noise: float = 0.2,
    base_ts: int = BASE_TS,
) -> pd.DataFrame:
    """
    Build spoofed OHLC with a linear trend from start_price to end_price.
    Each bar: open=prev close, close=trend + noise, high/low around open/close.
    """
    t = np.linspace(0, 1, num_bars)
    close = start_price + (end_price - start_price) * t + np.random.RandomState(42).randn(num_bars) * noise
    close = np.maximum(close, 0.01)
    open_ = np.roll(close, 1)
    open_[0] = start_price
    high = np.maximum(open_, close) + np.abs(np.random.RandomState(43).randn(num_bars)) * 0.1
    low = np.minimum(open_, close) - np.abs(np.random.RandomState(44).randn(num_bars)) * 0.1
    low = np.maximum(low, 0.01)
    timestamp = base_ts + np.arange(num_bars, dtype=np.int64) * 60
    volume = np.full(num_bars, 1000.0)
    return pd.DataFrame({
        "symbol": symbol,
        "timestamp": timestamp,
        "open": open_.astype(float),
        "high": high.astype(float),
        "low": low.astype(float),
        "close": close.astype(float),
        "volume": volume,
    })


def load_spoofed_into_duckdb(df: pd.DataFrame, connection: duckdb.DuckDBPyConnection) -> None:
    """Create trends table and insert the DataFrame. Replaces any existing data for the symbol(s)."""
    connection.execute("""
        CREATE OR REPLACE TABLE trends (
            symbol VARCHAR,
            timestamp BIGINT,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE
        )
    """)
    connection.register("_spoof", df)
    connection.execute("INSERT INTO trends SELECT * FROM _spoof")
    connection.unregister("_spoof")


def duckdb_with_spoofed_data(
    symbol: str,
    start_price: float,
    end_price: float,
    **make_ohlc_kwargs,
) -> duckdb.DuckDBPyConnection:
    """Return an in-memory DuckDB connection with trends table filled with spoofed data."""
    df = make_ohlc(symbol, start_price, end_price, **make_ohlc_kwargs)
    con = duckdb.connect(":memory:")
    load_spoofed_into_duckdb(df, con)
    return con
