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
        hist = hist.tz_localize("UTC", level=None, nonexistent="shift_forward", ambiguous="NaT") if hist.index.tz is None else hist
        df = hist.reset_index()
        return pd.DataFrame(
            {
                "symbol": symbol,
                "timestamp": df["Date"].astype("int64") // 10**9,
                "open": df["Open"],
                "high": df["High"],
                "low": df["Low"],
                "close": df["Close"],
                "volume": df["Volume"],
            }
        )
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

