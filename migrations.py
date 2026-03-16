"""
Database initialization / migrations for DuckDB.

Called once on startup to ensure required tables exist so the bot
doesn't rely on lazy creation inside other modules.
"""
import duckdb
from config import DB_PATH
from utils import logger
from trading import TRADE_LOG_TABLE, TRAIL_STATE_TABLE


def init_db() -> None:
    """Create core tables if they don't already exist."""
    con = duckdb.connect(DB_PATH)
    try:
        # Candle data table
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

        # Trade log (daily/weekly limits, PDT checks)
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRADE_LOG_TABLE} (
                timestamp_utc DOUBLE,
                symbol VARCHAR,
                side VARCHAR,
                qty DOUBLE
            )
            """
        )
        # Migration: ensure qty column exists even if table was created earlier without it
        try:
            con.execute(f"ALTER TABLE {TRADE_LOG_TABLE} ADD COLUMN qty DOUBLE")
        except Exception:
            pass

        # Trailing-stop state per symbol
        con.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRAIL_STATE_TABLE} (
                symbol VARCHAR PRIMARY KEY,
                running_high DOUBLE,
                updated_at DOUBLE
            )
            """
        )

        logger.info("Database initialized (tables ensured).")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise
    finally:
        con.close()

