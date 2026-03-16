"""
Unified data providers for candles with failover.

Primary: Massive (RESTClient aggregates)
Fallbacks: Yahoo Finance (yfinance), Finnhub (if FINNHUB_API_KEY set)
"""
import os
import time
from typing import List, Dict, Any

import requests
import pandas as pd
from massive import RESTClient
import yfinance as yf

from utils import logger


FINNHUB_BASE = "https://finnhub.io/api/v1/stock/candle"


_massive_api_key = os.getenv("MASSIVE_API_KEY")
_finnhub_api_key = os.getenv("FINNHUB_API_KEY")

_massive_client = RESTClient(api_key=_massive_api_key) if _massive_api_key else None


def _massive_supported() -> bool:
    return _massive_client is not None


def _fetch_from_massive_daily(symbol: str, start_ts: int, end_ts: int) -> pd.DataFrame | None:
    """
    Fetch daily candles from Massive between start_ts and end_ts (Unix seconds).
    Returns DataFrame or None on hard failure.
    """
    if not _massive_supported():
        return None
    try:
        start_ms = start_ts * 1000
        end_ms = end_ts * 1000
        # Massive stocks aggregates endpoint: 1 day bars
        # see https://massive.com/docs/rest/stocks/aggregates/ for latest details
        resp = _massive_client.stocks_aggregates(
            symbol=symbol,
            multiplier=1,
            timespan="day",
            from_=start_ms,
            to=end_ms,
        )
        results = getattr(resp, "results", None) or []
        if not results:
            return pd.DataFrame()
        rows: List[Dict[str, Any]] = []
        for r in results:
            # Massive uses fields like t (timestamp ms), o,h,l,c,v
            t_ms = getattr(r, "t", None) or r.get("t")
            if t_ms is None:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": int(t_ms // 1000),
                    "open": getattr(r, "o", None) or r.get("o"),
                    "high": getattr(r, "h", None) or r.get("h"),
                    "low": getattr(r, "l", None) or r.get("l"),
                    "close": getattr(r, "c", None) or r.get("c"),
                    "volume": getattr(r, "v", None) or r.get("v") or 0,
                }
            )
        return pd.DataFrame(rows)
    except Exception as e:
        logger.warning(f"Massive fetch failed for {symbol}: {e}")
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

    # Massive first
    df = _fetch_from_massive_daily(symbol, start_ts, end_ts)
    if df is not None:
        if len(df):
            logger.info(f"Fetched {len(df)} daily bars for {symbol} from Massive")
        return df

    # Yahoo Finance
    df = _fetch_from_yahoo_daily(symbol, start_ts, end_ts)
    if df is not None:
        if len(df):
            logger.info(f"Fetched {len(df)} daily bars for {symbol} from Yahoo Finance")
        return df

    # Finnhub as last resort (if configured)
    df = _fetch_from_finnhub_daily(symbol, start_ts, end_ts)
    if df is not None and len(df):
        logger.info(f"Fetched {len(df)} daily bars for {symbol} from Finnhub")
        return df

    logger.warning(f"No daily candles available for {symbol} from any provider")
    return pd.DataFrame()

