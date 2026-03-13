"""
Simple monitoring report: account equity, positions, P&L, and recent trade log.
Run on demand or on a schedule (e.g. after each bot run or via cron).
"""
import os
import time
import duckdb
from datetime import datetime, timezone
from dotenv import load_dotenv
from config import DB_PATH
from trading import trading_client, TRADE_LOG_TABLE

load_dotenv()


def _ensure_trade_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRADE_LOG_TABLE} (
            timestamp_utc DOUBLE,
            symbol VARCHAR,
            side VARCHAR
        )
    """)


def fetch_account_summary():
    """Return dict with equity, cash, buying_power; None if API fails."""
    try:
        account = trading_client.get_account()
        return {
            "equity": float(account.equity or 0),
            "cash": float(account.cash or 0),
            "buying_power": float(account.buying_power or 0),
        }
    except Exception as e:
        return None


def fetch_positions():
    """Return list of dicts: symbol, qty, entry_price, current_price, market_value, unrealized_pl."""
    try:
        positions = trading_client.get_all_positions()
        out = []
        for p in positions:
            qty = float(p.qty)
            if qty <= 0:
                continue
            try:
                entry = float(p.avg_entry_price or 0)
                current = float(p.current_price or p.market_value / qty if qty else 0)
                mv = float(p.market_value or 0)
                upl = float(p.unrealized_pl or 0)
            except (TypeError, ValueError):
                entry = current = mv = upl = 0
            out.append({
                "symbol": p.symbol,
                "qty": qty,
                "entry_price": entry,
                "current_price": current,
                "market_value": mv,
                "unrealized_pl": upl,
            })
        return out
    except Exception:
        return []


def fetch_recent_trades(limit: int = 20):
    """Return list of (timestamp_utc, symbol, side) from trade_log, newest first."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        rows = con.execute(f"""
            SELECT timestamp_utc, symbol, side
            FROM {TRADE_LOG_TABLE}
            ORDER BY timestamp_utc DESC
            LIMIT ?
        """, [limit]).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def fetch_daily_weekly_counts():
    """Return (daily_count, weekly_count) from trade_log."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        now = time.time()
        day_ago = now - 86400
        week_ago = now - 7 * 86400
        daily = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
            [day_ago],
        ).fetchone()[0]
        weekly = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
            [week_ago],
        ).fetchone()[0]
        return daily, weekly
    except Exception:
        return 0, 0
    finally:
        con.close()


def print_report():
    """Print a concise text summary to stdout."""
    account = fetch_account_summary()
    positions = fetch_positions()
    recent = fetch_recent_trades(20)
    daily, weekly = fetch_daily_weekly_counts()

    print("=" * 50)
    print("Trading Bot Report")
    print("=" * 50)
    if account:
        print(f"Equity:        ${account['equity']:,.2f}")
        print(f"Cash:          ${account['cash']:,.2f}")
        print(f"Buying power:  ${account['buying_power']:,.2f}")
    else:
        print("Account:       (unable to fetch - check Alpaca API)")
    print()
    print("Trade counts (rolling)")
    print(f"  Daily:       {daily}")
    print(f"  Weekly:      {weekly}")
    print()
    print("Open positions")
    if not positions:
        print("  (none)")
    else:
        total_pl = 0.0
        for p in positions:
            total_pl += p["unrealized_pl"]
            pl_str = f"${p['unrealized_pl']:+,.2f}"
            print(f"  {p['symbol']}: {p['qty']:.0f} @ ${p['entry_price']:.2f}  now ${p['current_price']:.2f}  P&L {pl_str}")
        print(f"  Total unrealized P&L: ${total_pl:+,.2f}")
    print()
    print("Recent trades (trade_log)")
    if not recent:
        print("  (none)")
    else:
        for ts, symbol, side in recent[:10]:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            print(f"  {dt}  {side:4}  {symbol}")
    print("=" * 50)


def main():
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        print("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.")
        return 1
    print_report()
    return 0


if __name__ == "__main__":
    exit(main())
