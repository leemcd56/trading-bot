"""
Unified data providers for candles and prices with failover.

Daily candles:
- Primary: Massive (Daily Ticker Summary via RESTClient.get_daily_open_close_agg) for latest EOD
- Historical + backfill: Yahoo Finance (yfinance), Finnhub (if FINNHUB_API_KEY set)

Intraday price:
- Primary: Yahoo Finance 1-minute bars
- Fallback: last stored close (via caller) or other providers if extended later
"""
import os
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import requests
import pandas as pd
from massive import RESTClient
import yfinance as yf

from utils import logger


FINNHUB_BASE = "https://finnhub.io/api/v1/stock/candle"


_massive_api_key = os.getenv("MASSIVE_API_KEY")
_finnhub_api_key = os.getenv("FINNHUB_API_KEY")
_massive_client = RESTClient(_massive_api_key) if _massive_api_key else None


def _massive_supported() -> bool:
    return _massive_client is not None


def _get_response_value(resp: Any, key: str, default: Any = None) -> Any:
    """Read from client response (dict or object)."""
    if hasattr(resp, "get"):
        return resp.get(key, default)
    return getattr(resp, key, default)


def _fetch_from_massive_daily(symbol: str, start_ts: int, end_ts: int) -> pd.DataFrame | None:
    """
    Fetch daily candles from Massive using RESTClient.get_daily_open_close_agg:
    Daily Ticker Summary = one ticker, one date; response has open, high, low, close, volume, from.
    """
    if not _massive_supported():
        return None
    rows: List[Dict[str, Any]] = []
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)
    current = start_dt.date()
    end_date = end_dt.date()
    while current <= end_date:
        date_str = current.strftime("%Y-%m-%d")
        try:
            request = _massive_client.get_daily_open_close_agg(
                symbol,
                date_str,
                adjusted="true",
            )
        except Exception as e:
            # Auth (401/403) or server errors: fail fast so we can fall back to Yahoo
            err_str = str(e).lower()
            if "401" in err_str or "403" in err_str or "unauthorized" in err_str or "forbidden" in err_str:
                logger.warning(f"Massive auth failed; skipping provider: {e}")
                return None
            if "429" in err_str or "rate" in err_str:
                logger.warning("Massive rate limit (429); backing off")
                time.sleep(60)
                continue
            logger.warning(f"Massive request failed for {symbol} {date_str}: {e}")
            current = current + timedelta(days=1)
            continue

        status = _get_response_value(request, "status")
        if status and status != "OK":
            current = current + timedelta(days=1)
            continue
        open_ = _get_response_value(request, "open")
        high = _get_response_value(request, "high")
        low = _get_response_value(request, "low")
        close = _get_response_value(request, "close")
        volume = _get_response_value(request, "volume") or 0
        from_str = _get_response_value(request, "from") or date_str
        if close is None:
            current = current + timedelta(days=1)
            continue
        try:
            dt = datetime.strptime(from_str, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
            ts = int(dt.timestamp())
        except (ValueError, TypeError):
            ts = int(datetime.combine(current, datetime.min.time()).replace(tzinfo=timezone.utc).timestamp()) + 86400 - 1
        rows.append({
            "symbol": symbol,
            "timestamp": ts,
            "open": float(open_) if open_ is not None else float(close),
            "high": float(high) if high is not None else float(close),
            "low": float(low) if low is not None else float(close),
            "close": float(close),
            "volume": float(volume) if volume is not None else 0,
        })
        current = current + timedelta(days=1)
        time.sleep(12)  # Free tier ~5 req/min
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


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

    # 2) Optionally overlay latest EOD from Massive for the most recent market day
    latest_eod = _fetch_from_massive_daily(symbol, start_ts, end_ts)
    if latest_eod is not None and len(latest_eod) > 0:
        # Keep everything except rows on/after Massive's last timestamp, then append Massive rows
        latest_ts = int(latest_eod["timestamp"].max())
        if not df.empty:
            df = df[df["timestamp"] < latest_ts]
        df = pd.concat([df, latest_eod], ignore_index=True).sort_values("timestamp")
        logger.info(f"Overlayed latest EOD for {symbol} from Massive")

    # 3) If still empty, try Finnhub as last resort
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

