"""
Database initialization / migrations for DuckDB.

Called once on startup to ensure required tables exist so the bot
doesn't rely on lazy creation inside other modules.
"""
import duckdb
from config import DB_PATH
from utils import logger
from trading import TRADE_LOG_TABLE, TRAIL_STATE_TABLE


def _ensure_trends_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Ensure trends table exists with canonical column types."""
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
    cols = con.execute("PRAGMA table_info('trends')").fetchall()
    col_types = {str(row[1]).lower(): str(row[2]).upper() for row in cols}
    ts_type = col_types.get("timestamp")
    int_types = {
        "BIGINT",
        "HUGEINT",
        "INTEGER",
        "INT",
        "INT64",
        "LONG",
        "UBIGINT",
        "UINTEGER",
    }
    needs_rebuild = ts_type not in int_types
    if not needs_rebuild:
        return

    logger.warning(
        "Detected non-integer trends.timestamp type (%s); rebuilding trends table.",
        ts_type,
    )
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute(
            """
            CREATE TABLE trends_new (
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
        con.execute(
            """
            INSERT INTO trends_new (symbol, timestamp, open, high, low, close, volume)
            SELECT
                symbol,
                TRY_CAST(timestamp AS BIGINT) AS timestamp,
                TRY_CAST(open AS DOUBLE) AS open,
                TRY_CAST(high AS DOUBLE) AS high,
                TRY_CAST(low AS DOUBLE) AS low,
                TRY_CAST(close AS DOUBLE) AS close,
                TRY_CAST(volume AS DOUBLE) AS volume
            FROM trends
            WHERE TRY_CAST(timestamp AS BIGINT) IS NOT NULL
              AND TRY_CAST(timestamp AS BIGINT) >= 1000000000
            """
        )
        copied_rows = con.execute("SELECT COUNT(*) FROM trends_new").fetchone()[0]
        con.execute("DROP TABLE trends")
        con.execute("ALTER TABLE trends_new RENAME TO trends")
        con.execute("COMMIT")
        logger.info(
            "Rebuilt trends table with BIGINT timestamps; retained %s rows.",
            int(copied_rows),
        )
    except Exception:
        con.execute("ROLLBACK")
        raise


def init_db() -> None:
    """Create core tables if they don't already exist."""
    con = duckdb.connect(DB_PATH)
    try:
        # Candle data table + schema guard/migration for legacy bad timestamp types.
        _ensure_trends_schema(con)

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

