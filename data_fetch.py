"""
Fetch 1-min candle data from Finnhub and upsert into DuckDB table `trends`.
"""
import os
import time
import requests
import duckdb
import pandas as pd
from config import DB_PATH
from utils import logger

FINNHUB_BASE = "https://finnhub.io/api/v1/stock/candle"
# Request last ~5 trading days of 1-min bars (390 min/day * 5 ≈ 2000)
LOOKBACK_MINUTES = 2000


def fetch_and_store(symbol: str) -> None:
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        logger.warning("FINNHUB_API_KEY not set - skipping fetch")
        return

    tz = pytz.timezone("US/Eastern")
    now = time.time()
    from_ts = int(now - LOOKBACK_MINUTES * 60)

    params = {
        "symbol": symbol,
        "resolution": "1",
        "from": from_ts,
        "to": int(now),
        "token": api_key,
    }
    try:
        r = requests.get(FINNHUB_BASE, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException as e:
        logger.error(f"Finnhub request failed for {symbol}: {e}")
        return
    except Exception as e:
        logger.error(f"Unexpected error fetching {symbol}: {e}")
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
