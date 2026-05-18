"""
Web dashboard for the trading bot.
Run: uvicorn dashboard:app --port 8080
"""
from datetime import datetime
from pathlib import Path

import duckdb
import pytz
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

load_dotenv()

from config import DB_PATH, MAX_DAILY_TRADES, MAX_WEEKLY_TRADES, TRADING_MODE
from report import fetch_account_summary, fetch_daily_weekly_counts, fetch_positions
from trading import TRADE_HISTORY_TABLE

_ET = pytz.timezone("US/Eastern")
_HTML = Path(__file__).parent / "dashboard.html"
_SNAPSHOTS = "portfolio_snapshots"

app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)


@app.get("/")
def root():
    return FileResponse(_HTML, media_type="text/html")


@app.get("/api/summary")
def api_summary():
    account = fetch_account_summary()
    if not account:
        return JSONResponse({"error": "Unable to fetch account"}, status_code=503)
    today_et = datetime.now(_ET).strftime("%Y-%m-%d")
    con = duckdb.connect(DB_PATH)
    try:
        row = con.execute(
            f"SELECT equity FROM {_SNAPSHOTS} WHERE date_et = ? AND label = 'open' LIMIT 1",
            [today_et],
        ).fetchone()
        account["open_equity"] = float(row[0]) if row else None
    except Exception:
        account["open_equity"] = None
    finally:
        con.close()
    return account


@app.get("/api/positions")
def api_positions():
    return fetch_positions()


@app.get("/api/transactions")
def api_transactions(limit: int = 25):
    con = duckdb.connect(DB_PATH)
    try:
        rows = con.execute(
            f"""
            SELECT timestamp_utc, symbol, side, qty, price, source
            FROM {TRADE_HISTORY_TABLE}
            ORDER BY timestamp_utc DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
        return [
            {
                "timestamp_utc": r[0],
                "symbol": r[1],
                "side": r[2],
                "qty": r[3],
                "price": r[4],
                "source": r[5],
            }
            for r in rows
        ]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        con.close()


@app.get("/api/portfolio-history")
def api_portfolio_history():
    con = duckdb.connect(DB_PATH)
    try:
        rows = con.execute(
            f"""
            SELECT
                date_et,
                COALESCE(
                    MAX(CASE WHEN label = 'close' THEN equity END),
                    MAX(CASE WHEN label = 'open'  THEN equity END)
                ) AS equity
            FROM {_SNAPSHOTS}
            GROUP BY date_et
            ORDER BY date_et DESC
            LIMIT 7
            """
        ).fetchall()
        return [{"date": r[0], "equity": r[1]} for r in reversed(rows)]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    finally:
        con.close()


@app.get("/api/mode")
def api_mode():
    return {"mode": TRADING_MODE}


@app.get("/api/trade-counts")
def api_trade_counts():
    daily, weekly = fetch_daily_weekly_counts()
    return {
        "daily": daily,
        "daily_max": MAX_DAILY_TRADES,
        "weekly": weekly,
        "weekly_max": MAX_WEEKLY_TRADES,
    }
