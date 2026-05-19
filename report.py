"""
Simple monitoring report: account equity, positions, P&L, and recent trade log.
Run on demand or on a schedule (e.g. after each bot run or via cron).
"""
import os
import time
import duckdb
import pytz
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from config import DB_PATH
from trading import trading_client, TRADE_LOG_TABLE, TRADE_HISTORY_TABLE

load_dotenv()

PORTFOLIO_SNAPSHOTS_TABLE = "portfolio_snapshots"
_ET = pytz.timezone("US/Eastern")


def _ensure_trade_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRADE_LOG_TABLE} (
            timestamp_utc DOUBLE,
            symbol VARCHAR,
            side VARCHAR
        )
    """)


def _ensure_portfolio_snapshots(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {PORTFOLIO_SNAPSHOTS_TABLE} (
            timestamp_utc DOUBLE,
            date_et VARCHAR,
            label VARCHAR,
            equity DOUBLE
        )
    """)


def _ensure_trade_history(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRADE_HISTORY_TABLE} (
            timestamp_utc DOUBLE,
            symbol VARCHAR,
            side VARCHAR,
            qty DOUBLE,
            price DOUBLE,
            source VARCHAR
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
        now_et = datetime.now(_ET)
        midnight_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        day_start_ts = midnight_et.timestamp()
        week_ago_ts = midnight_et.timestamp() - 7 * 86400
        daily = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
            [day_start_ts],
        ).fetchone()[0]
        weekly = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
            [week_ago_ts],
        ).fetchone()[0]
        return daily, weekly
    except Exception:
        return 0, 0
    finally:
        con.close()


def snapshot_portfolio(label: str, _retries: int = 3, _backoff: float = 5.0) -> float | None:
    """
    Fetch current equity from Alpaca and store it in portfolio_snapshots.
    Idempotent: returns None (and skips the insert) if this label already
    exists for today's ET date. Returns the equity on first call.
    Retries on transient MotherDuck CatalogException errors.
    """
    date_et = datetime.now(_ET).strftime("%Y-%m-%d")
    for attempt in range(_retries):
        con = duckdb.connect(DB_PATH)
        try:
            _ensure_portfolio_snapshots(con)
            if con.execute(
                f"SELECT 1 FROM {PORTFOLIO_SNAPSHOTS_TABLE} WHERE date_et = ? AND label = ? LIMIT 1",
                [date_et, label],
            ).fetchone():
                return None
            account = fetch_account_summary()
            if not account:
                return None
            equity = account["equity"]
            con.execute(
                f"INSERT INTO {PORTFOLIO_SNAPSHOTS_TABLE} (timestamp_utc, date_et, label, equity) VALUES (?, ?, ?, ?)",
                [time.time(), date_et, label, equity],
            )
            return equity
        except duckdb.CatalogException:
            con.close()
            if attempt < _retries - 1:
                time.sleep(_backoff * (attempt + 1))
            else:
                raise
        finally:
            try:
                con.close()
            except Exception:
                pass


def fetch_todays_trades() -> list:
    """Return today's trade_history rows (ET calendar day), oldest first."""
    today_start_et = datetime.now(_ET).replace(hour=0, minute=0, second=0, microsecond=0)
    start_ts = today_start_et.timestamp()
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_history(con)
        return con.execute(
            f"SELECT timestamp_utc, symbol, side, qty, price, source FROM {TRADE_HISTORY_TABLE} WHERE timestamp_utc >= ? ORDER BY timestamp_utc",
            [start_ts],
        ).fetchall()
    finally:
        con.close()


def _fetch_todays_snapshots() -> tuple[float | None, float | None]:
    """Return (open_equity, close_equity) for today's ET date."""
    date_et = datetime.now(_ET).strftime("%Y-%m-%d")
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_portfolio_snapshots(con)
        open_row = con.execute(
            f"SELECT equity FROM {PORTFOLIO_SNAPSHOTS_TABLE} WHERE date_et = ? AND label = 'open'",
            [date_et],
        ).fetchone()
        close_row = con.execute(
            f"SELECT equity FROM {PORTFOLIO_SNAPSHOTS_TABLE} WHERE date_et = ? AND label = 'close'",
            [date_et],
        ).fetchone()
        return (float(open_row[0]) if open_row else None, float(close_row[0]) if close_row else None)
    finally:
        con.close()


def send_eod_summary() -> None:
    """Build and send an end-of-day Discord embed summarising trades and portfolio performance."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return

    trades = fetch_todays_trades()
    open_equity, close_equity = _fetch_todays_snapshots()
    positions = fetch_positions()
    date_str = datetime.now(_ET).strftime("%B %d, %Y")

    lines = []

    # Portfolio performance
    if open_equity and close_equity:
        delta = close_equity - open_equity
        pct = delta / open_equity * 100
        arrow = "📈" if delta >= 0 else "📉"
        sign = "+" if delta >= 0 else ""
        lines.append(f"{arrow} **Portfolio:** ${open_equity:,.2f} → ${close_equity:,.2f} ({sign}${delta:,.2f}, {sign}{pct:.2f}%)")
    elif close_equity:
        lines.append(f"💰 **Portfolio at close:** ${close_equity:,.2f}")
    elif open_equity:
        lines.append(f"💰 **Portfolio at open:** ${open_equity:,.2f}")

    # Today's trades
    lines.append("")
    if trades:
        lines.append(f"**Trades today ({len(trades)}):**")
        for ts, symbol, side, qty, price, source in trades:
            time_et = datetime.fromtimestamp(ts, tz=_ET).strftime("%I:%M %p")
            tag = f" [{source}]" if source != "ta" else ""
            if qty and qty > 0:
                qty_str = f"{qty:.4g} sh"
            else:
                qty_str = "notional"
            price_str = f" @ ${price:.2f}" if price else ""
            emoji = "🟢" if side == "BUY" else "🔴"
            lines.append(f"{emoji} {time_et}  **{side}** {symbol}  {qty_str}{price_str}{tag}")
    else:
        lines.append("No trades executed today.")

    # Open positions
    if positions:
        lines.append("")
        lines.append(f"**Open positions ({len(positions)}):**")
        for p in positions:
            pl = p["unrealized_pl"]
            sign = "+" if pl >= 0 else ""
            lines.append(f"  • **{p['symbol']}** {p['qty']:.4g} sh @ ${p['entry_price']:.2f}  |  P&L {sign}${pl:.2f}")

    description = "\n".join(lines)
    if len(description) > 4096:
        description = description[:4093] + "..."

    if open_equity and close_equity:
        color = 0x2ECC71 if (close_equity >= open_equity) else 0xE74C3C
    else:
        color = 0x3498DB

    payload = {
        "embeds": [{
            "title": f"📊 Daily Summary — {date_str}",
            "description": description,
            "color": color,
        }]
    }
    try:
        requests.post(webhook_url, json=payload, timeout=5)
    except Exception:
        pass


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
