"""
Fetch daily candle data via unified providers and upsert into DuckDB table `trends`.
"""
import time
import duckdb
from config import DB_PATH, TRENDS_RETAIN_DAYS
from utils import logger
from data_providers import get_daily_candles_with_failover


def fetch_and_store(symbol: str) -> None:
    df = get_daily_candles_with_failover(symbol)
    if df is None or len(df) == 0:
        logger.warning(f"No candle data returned for {symbol} from any provider")
        return
    # Require at least one valid close so we never store all-null OHLC
    if df["close"].isna().all():
        logger.warning(f"Refusing to store {symbol}: all close values are null")
        return

    con = duckdb.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS trends (
            symbol VARCHAR,
            timestamp BIGINT,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE
        )
        """
    )

    # Upsert: delete overlapping (symbol, timestamp) then insert via DataFrame
    con.register("_new_bars", df)
    con.execute(
        """
        DELETE FROM trends
        WHERE symbol = (SELECT symbol FROM _new_bars LIMIT 1)
          AND timestamp IN (SELECT timestamp FROM _new_bars)
        """
    )
    con.execute(
        """
        INSERT INTO trends (symbol, timestamp, open, high, low, close, volume)
        SELECT symbol, timestamp, open, high, low, close, volume FROM _new_bars
        """
    )
    con.unregister("_new_bars")
    # Log basic stats so we can debug what the DB actually contains in environments
    # where we can't open DuckDB directly (e.g. Railway containers).
    try:
        stats = con.execute(
            """
            SELECT symbol,
                   COUNT(*)      AS n,
                   MIN(timestamp) AS min_ts,
                   MAX(timestamp) AS max_ts
            FROM trends
            GROUP BY symbol
            """
        ).fetchdf()
        logger.info(
            f"Fetched and stored {len(df)} bars for {symbol}; "
            f"trends stats: {stats.to_dict(orient='records')}"
        )
    except Exception as e:
        logger.warning(f"Failed to log trends stats after upsert for {symbol}: {e}")
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
