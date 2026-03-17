"""
Unified data providers for candles and prices with failover.

Daily candles:
- Primary: Yahoo Finance (yfinance) for history
- Fallback: Finnhub (if FINNHUB_API_KEY set)

Intraday price:
- Primary: Yahoo Finance 1-minute bars
- Fallback: last stored close (via caller) or other providers if extended later

Note: Massive is currently used only via the Python client for other potential
use cases; to stay within the Massive Free tier, we do NOT loop over daily
history with Daily Ticker Summary anymore.
"""
import os
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import requests
import pandas as pd
import yfinance as yf

from utils import logger


FINNHUB_BASE = "https://finnhub.io/api/v1/stock/candle"


_finnhub_api_key = os.getenv("FINNHUB_API_KEY")


def _col(df: pd.DataFrame, *candidates: str):
    """Return first existing column from df (case-insensitive). If column is a DataFrame (MultiIndex), take .iloc[:, 0]."""
    def norm(c):
        if isinstance(c, tuple):
            return str(c[0]).lower() if c else ""
        return str(c).lower()
    col_map = {norm(c): c for c in df.columns}
    for name in candidates:
        key = name.lower()
        if key in col_map:
            col = df[col_map[key]]
            if isinstance(col, pd.DataFrame):
                col = col.iloc[:, 0]
            return col
    return None


def _fetch_from_yahoo_daily(symbol: str, start_ts: int, end_ts: int) -> pd.DataFrame | None:
    """
    Fetch daily candles from Yahoo Finance (yfinance).
    Returns DataFrame or None on hard failure.
    """
    try:
        start = pd.to_datetime(start_ts, unit="s", utc=True)
        end = pd.to_datetime(end_ts, unit="s", utc=True)
        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start, end=end, interval="1d", auto_adjust=False)
        if hist.empty:
            return pd.DataFrame()
        # Ensure the index is UTC datetimes
        if hist.index.tz is None:
            hist.index = hist.index.tz_localize("UTC", nonexistent="shift_forward", ambiguous="NaT")
        else:
            hist.index = hist.index.tz_convert("UTC")

        df = hist.copy()
        # Use the datetime index directly for timestamps so we never confuse it with a RangeIndex
        timestamps = pd.to_datetime(df.index, utc=True).astype("int64") // 10**9

        open_col = _col(df, "Open", "open")
        high_col = _col(df, "High", "high")
        low_col = _col(df, "Low", "low")
        close_col = _col(df, "Close", "close")
        vol_col = _col(df, "Volume", "volume")

        if close_col is None:
            logger.warning(
                f"Yahoo Finance: missing close column for {symbol} (columns: {list(df.columns)})"
            )
            return None
        if open_col is None:
            open_col = close_col  # fallback
        if high_col is None:
            high_col = close_col
        if low_col is None:
            low_col = close_col
        if vol_col is None:
            vol_col = pd.Series(0.0, index=df.index)

        out = pd.DataFrame(
            {
                "symbol": symbol,
                "timestamp": timestamps,
                "open": pd.to_numeric(open_col, errors="coerce"),
                "high": pd.to_numeric(high_col, errors="coerce"),
                "low": pd.to_numeric(low_col, errors="coerce"),
                "close": pd.to_numeric(close_col, errors="coerce"),
                "volume": pd.to_numeric(vol_col, errors="coerce").fillna(0),
            }
        )
        # Drop rows with no valid close so we never store all-null OHLC
        out = out.dropna(subset=["close"])
        if out.empty:
            logger.warning(f"Yahoo Finance: no valid OHLC for {symbol}")
            return pd.DataFrame()
        return out
    except Exception as e:
        logger.warning(f"Yahoo Finance fetch failed for {symbol}: {e}")
        return None


def _fetch_from_finnhub_daily(symbol: str, start_ts: int, end_ts: int) -> pd.DataFrame | None:
    """
    Fetch daily candles from Finnhub, if configured.
    Returns DataFrame or None on hard failure.
    """
    api_key = _finnhub_api_key
    if not api_key:
        return None
    params = {
        "symbol": symbol,
        "resolution": "D",
        "from": start_ts,
        "to": end_ts,
        "token": api_key,
    }
    try:
        r = requests.get(FINNHUB_BASE, params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            logger.warning(f"Finnhub error {r.status_code} for {symbol}")
            return None
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"Finnhub fetch failed for {symbol}: {e}")
        return None

    if not data.get("t") or not data.get("c"):
        return pd.DataFrame()

    return pd.DataFrame(
        {
            "symbol": symbol,
            "timestamp": data["t"],
            "open": data["o"],
            "high": data["h"],
            "low": data["l"],
            "close": data["c"],
            "volume": data.get("v", [0] * len(data["t"])),
        }
    )


def get_daily_candles_with_failover(symbol: str, lookback_days: int = 365) -> pd.DataFrame:
    """
    Fetch recent daily candles for a symbol with provider failover.
    Tries Massive → Yahoo Finance → Finnhub.
    Returns empty DataFrame if all fail or no data.
    """
    now = time.time()
    start_ts = int(now - lookback_days * 86400)
    end_ts = int(now)

    # 1) Historical window from Yahoo first (cheapest/free for history)
    df = _fetch_from_yahoo_daily(symbol, start_ts, end_ts)
    if df is None:
        df = pd.DataFrame()

    # 2) If still empty, try Finnhub as last resort
    if df.empty:
        df_fh = _fetch_from_finnhub_daily(symbol, start_ts, end_ts)
        if df_fh is not None and len(df_fh):
            logger.info(f"Fetched {len(df_fh)} daily bars for {symbol} from Finnhub")
            return df_fh

    if df.empty:
        logger.warning(f"No daily candles available for {symbol} from any provider")
    return df.reset_index(drop=True)


def get_intraday_price(symbol: str) -> Optional[float]:
    """
    Fetch a recent intraday price for a symbol.
    Primary: Yahoo Finance 1-minute bars for today, last Close.
    Returns None on failure; callers should fall back to last daily close if needed.
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1d", interval="1m", auto_adjust=False)
        if hist.empty or "Close" not in hist.columns:
            return None
        price = float(hist["Close"].iloc[-1])
        if price <= 0:
            return None
        return price
    except Exception as e:
        logger.warning(f"Intraday price fetch failed for {symbol} via Yahoo: {e}")
        return None

