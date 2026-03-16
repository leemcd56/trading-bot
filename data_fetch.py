"""
Fetch daily candle data from Finnhub and upsert into DuckDB table `trends`.
"""
import os
import time
import requests
import duckdb
import pandas as pd
from config import DB_PATH, TRENDS_RETAIN_DAYS
from utils import logger

FINNHUB_BASE = "https://finnhub.io/api/v1/stock/candle"
LOOKBACK_DAYS = 365  # how many days of history to request
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2  # exponential: 2, 4, 8


def fetch_and_store(symbol: str) -> None:
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        logger.warning("FINNHUB_API_KEY not set - skipping fetch")
        return

    now = time.time()
    from_ts = int(now - LOOKBACK_DAYS * 86400)
    params = {
        "symbol": symbol,
        "resolution": "D",  # daily candles for Finnhub free tier
        "from": from_ts,
        "to": int(now),
        "token": api_key,
    }

    data = None
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(FINNHUB_BASE, params=params, timeout=30)
            if r.status_code == 429:
                wait = RETRY_BACKOFF_SEC ** (attempt + 1)
                logger.warning(f"Finnhub rate limit (429) for {symbol}, retry in {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                wait = RETRY_BACKOFF_SEC ** (attempt + 1)
                logger.warning(f"Finnhub server error {r.status_code} for {symbol}, retry in {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            break
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"Finnhub request failed for {symbol} after {MAX_RETRIES} attempts: {e}")
                return
            wait = RETRY_BACKOFF_SEC ** (attempt + 1)
            logger.warning(f"Finnhub request failed for {symbol}: {e}, retry in {wait}s")
            time.sleep(wait)
        except Exception as e:
            logger.error(f"Unexpected error fetching {symbol}: {e}")
            return

    if data is None:
        return

    if not data.get("t") or not data.get("c"):
        logger.warning(f"No candle data returned for {symbol}")
        return

    df = pd.DataFrame({
        "symbol": symbol,
        "timestamp": data["t"],
        "open": data["o"],
        "high": data["h"],
        "low": data["l"],
        "close": data["c"],
        "volume": data.get("v", [0] * len(data["t"])),
    })

    con = duckdb.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trends (
            symbol VARCHAR,
            timestamp BIGINT,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE
        )
    """)

    if len(df) == 0:
        con.close()
        return

    # Upsert: delete overlapping (symbol, timestamp) then insert via DataFrame
    con.register("_new_bars", df)
    con.execute("""
        DELETE FROM trends
        WHERE symbol = (SELECT symbol FROM _new_bars LIMIT 1)
          AND timestamp IN (SELECT timestamp FROM _new_bars)
    """)
    con.execute("""
        INSERT INTO trends (symbol, timestamp, open, high, low, close, volume)
        SELECT symbol, timestamp, open, high, low, close, volume FROM _new_bars
    """)
    con.unregister("_new_bars")
    logger.info(f"Fetched and stored {len(df)} bars for {symbol}")
    con.close()


def prune_old_trends() -> None:
    """Delete candle rows older than TRENDS_RETAIN_DAYS to limit DB size."""
    if TRENDS_RETAIN_DAYS <= 0:
        return
    con = duckdb.connect(DB_PATH)
    try:
        cutoff = int(time.time()) - TRENDS_RETAIN_DAYS * 86400
        con.execute("DELETE FROM trends WHERE timestamp < ?", [cutoff])
        logger.debug(f"Pruned trends older than {TRENDS_RETAIN_DAYS} days")
    except Exception as e:
        logger.warning(f"Prune trends failed: {e}")
    finally:
        con.close()
