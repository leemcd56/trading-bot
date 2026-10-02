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
from data_providers import get_daily_candles_with_failover, get_intraday_price

load_dotenv()

PORTFOLIO_SNAPSHOTS_TABLE = "portfolio_snapshots"
_ET = pytz.timezone("US/Eastern")

# Buy-and-hold comparison symbol for fetch_benchmark_comparison() - the honest
# check on whether active trading is earning its complexity over the simplest
# possible alternative. Override with BENCHMARK_SYMBOL in .env (e.g. a
# different index fund); this is a reporting preference, not a risk
# parameter, so it lives here rather than in the per-mode config system.
BENCHMARK_SYMBOL = os.getenv("BENCHMARK_SYMBOL", "SPY").strip().upper() or "SPY"


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
        return None


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
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ? AND side = 'BUY'",
            [day_start_ts],
        ).fetchone()[0]
        weekly = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ? AND side = 'BUY'",
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


def _first_portfolio_snapshot() -> tuple[float, str, float] | None:
    """Return (timestamp_utc, date_et, equity) of the earliest recorded 'open' snapshot, or None."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_portfolio_snapshots(con)
        row = con.execute(
            f"SELECT timestamp_utc, date_et, equity FROM {PORTFOLIO_SNAPSHOTS_TABLE} "
            "WHERE label = 'open' ORDER BY timestamp_utc ASC LIMIT 1"
        ).fetchone()
        if not row or row[2] is None or float(row[2]) <= 0:
            return None
        return float(row[0]), str(row[1]), float(row[2])
    finally:
        con.close()


def fetch_benchmark_comparison(benchmark_symbol: str | None = None) -> dict | None:
    """
    Compare the bot's return since its first recorded equity snapshot against
    a simple buy-and-hold of `benchmark_symbol` (default BENCHMARK_SYMBOL,
    e.g. SPY) over the same span - the honest check on whether active trading
    is earning its complexity over the simplest possible alternative.

    Returns None if there's no inception snapshot yet (brand new bot), the
    current account can't be read, or the benchmark's price history can't be
    fetched - all data-availability gaps, not reasons to show a wrong number.
    """
    symbol = (benchmark_symbol or BENCHMARK_SYMBOL).strip().upper()

    first_snapshot = _first_portfolio_snapshot()
    if first_snapshot is None:
        return None
    inception_ts, inception_date_et, inception_equity = first_snapshot

    account = fetch_account_summary()
    if not account:
        return None
    current_equity = account["equity"]

    lookback_days = max(int((time.time() - inception_ts) / 86400) + 5, 5)
    candles = get_daily_candles_with_failover(symbol, lookback_days=lookback_days)
    if candles is None or candles.empty:
        return None
    candles = candles.sort_values("timestamp")
    on_or_after_inception = candles[candles["timestamp"] >= inception_ts]
    if on_or_after_inception.empty:
        return None
    benchmark_start_price = float(on_or_after_inception.iloc[0]["close"])
    if benchmark_start_price <= 0:
        return None

    benchmark_current_price = get_intraday_price(symbol)
    if not benchmark_current_price or benchmark_current_price <= 0:
        benchmark_current_price = float(candles.iloc[-1]["close"])

    bot_return_pct = (current_equity - inception_equity) / inception_equity * 100
    benchmark_return_pct = (benchmark_current_price - benchmark_start_price) / benchmark_start_price * 100

    return {
        "benchmark_symbol": symbol,
        "inception_date_et": inception_date_et,
        "inception_equity": inception_equity,
        "current_equity": current_equity,
        "bot_return_pct": bot_return_pct,
        "benchmark_start_price": benchmark_start_price,
        "benchmark_current_price": benchmark_current_price,
        "benchmark_return_pct": benchmark_return_pct,
        "alpha_pct": bot_return_pct - benchmark_return_pct,
    }


def send_eod_summary() -> None:
    """Build and send an end-of-day Discord embed summarising trades and portfolio performance."""
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return

    trades = fetch_todays_trades()
    open_equity, close_equity = _fetch_todays_snapshots()
    positions = fetch_positions() or []
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

    # Since-inception benchmark comparison - is active trading beating a simple buy-and-hold?
    benchmark = fetch_benchmark_comparison()
    if benchmark:
        sign_bot = "+" if benchmark["bot_return_pct"] >= 0 else ""
        sign_bm = "+" if benchmark["benchmark_return_pct"] >= 0 else ""
        sign_alpha = "+" if benchmark["alpha_pct"] >= 0 else ""
        lines.append(
            f"📐 **Since {benchmark['inception_date_et']}:** Bot {sign_bot}{benchmark['bot_return_pct']:.2f}% "
            f"vs {benchmark['benchmark_symbol']} (buy & hold) {sign_bm}{benchmark['benchmark_return_pct']:.2f}% "
            f"({sign_alpha}{benchmark['alpha_pct']:.2f} pts)"
        )

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
    positions = fetch_positions() or []
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
    print()
    benchmark = fetch_benchmark_comparison()
    print(f"Since inception (vs. buy-and-hold {benchmark['benchmark_symbol'] if benchmark else BENCHMARK_SYMBOL})")
    if benchmark:
        sign_bot = "+" if benchmark["bot_return_pct"] >= 0 else ""
        sign_bm = "+" if benchmark["benchmark_return_pct"] >= 0 else ""
        sign_alpha = "+" if benchmark["alpha_pct"] >= 0 else ""
        print(f"  Since:         {benchmark['inception_date_et']}")
        print(f"  Bot return:    {sign_bot}{benchmark['bot_return_pct']:.2f}%")
        print(f"  {benchmark['benchmark_symbol']} return:    {sign_bm}{benchmark['benchmark_return_pct']:.2f}%")
        print(f"  Difference:    {sign_alpha}{benchmark['alpha_pct']:.2f} pts")
    else:
        print("  (not enough data yet - need at least one recorded equity snapshot and a reachable price feed)")
    print("=" * 50)


def main():
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        print("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.")
        return 1
    print_report()
    return 0


if __name__ == "__main__":
    exit(main())
