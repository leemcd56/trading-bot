"""
Backtest the current strategy on historical data.
Reuses analyze_trends() and mirrors trading.py entry/exit logic.
Reads from trends_backtest table by default (populate via backfill.py).
"""
import argparse
import csv
import time
import duckdb
import pandas as pd
from config import (
    DB_PATH,
    MAX_DAILY_TRADES,
    MAX_WEEKLY_TRADES,
    MAX_OPEN_POSITIONS,
    STOP_LOSS_PCT,
)
from analysis import analyze_trends
from utils import logger

TRADE_LOG_TABLE = "trade_log"
DEFAULT_TABLE = "trends_backtest"
BARS_PER_CHECK = 10  # simulate 10-min checks


def _ensure_trade_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRADE_LOG_TABLE} (
            timestamp_utc DOUBLE,
            symbol VARCHAR,
            side VARCHAR
        )
    """)


def _count_daily_sim(log_con: duckdb.DuckDBPyConnection, now_ts: float) -> int:
    day_ago = now_ts - 86400
    out = log_con.execute(
        f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
        [day_ago],
    ).fetchone()
    return out[0] if out else 0


def _count_weekly_sim(log_con: duckdb.DuckDBPyConnection, now_ts: float) -> int:
    week_ago = now_ts - 7 * 86400
    out = log_con.execute(
        f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
        [week_ago],
    ).fetchone()
    return out[0] if out else 0


def run_backtest(
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_capital: float = 100_000.0,
    table: str = DEFAULT_TABLE,
    equity_curve_path: str | None = None,
) -> dict:
    """
    Run backtest and return metrics dict: total_return_pct, max_drawdown_pct,
    num_trades, win_rate, trades (list of dicts).
    """
    con = duckdb.connect(DB_PATH)
    try:
        start_ts = int(time.mktime(time.strptime(start_date, "%Y-%m-%d")))
        end_ts = int(time.mktime(time.strptime(end_date, "%Y-%m-%d"))) + 86400
    except ValueError as e:
        raise ValueError(f"Invalid date format (use YYYY-MM-DD): {e}") from e

    # Load all bars for symbols in range
    placeholders = ",".join(["?"] * len(symbols))
    query = f"""
        SELECT symbol, timestamp, open, high, low, close, volume
        FROM {table}
        WHERE symbol IN ({placeholders}) AND timestamp >= ? AND timestamp < ?
        ORDER BY timestamp ASC
    """
    params = list(symbols) + [start_ts, end_ts]
    try:
        df_all = con.execute(query, params).fetchdf()
    except Exception as e:
        logger.error(f"Load backtest data failed. Run backfill.py first? {e}")
        con.close()
        raise

    if len(df_all) == 0:
        con.close()
        return {
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "num_trades": 0,
            "win_rate": 0.0,
            "trades": [],
            "final_equity": initial_capital,
        }

    # In-memory connection for feeding slices to analyze_trends
    mem_con = duckdb.connect(":memory:")
    # Separate in-memory connection for simulated trade log (do not touch main DB)
    log_con = duckdb.connect(":memory:")
    _ensure_trade_log(log_con)

    cash = initial_capital
    positions: dict[str, dict] = {}  # symbol -> {qty, entry_price}
    trades: list[dict] = []
    equity_curve: list[tuple[float, float]] = []
    peak_equity = initial_capital
    max_drawdown_pct = 0.0

    unique_ts = sorted(df_all["timestamp"].unique())
    n_steps = len(unique_ts) // BARS_PER_CHECK
    if n_steps == 0:
        n_steps = 1

    for step in range(n_steps):
        end_idx = min((step + 1) * BARS_PER_CHECK, len(unique_ts))
        now_ts = float(unique_ts[end_idx - 1])

        current_prices: dict[str, float] = {}
        analyses: dict[str, dict] = {}

        for symbol in symbols:
            df_sym = df_all[df_all["symbol"] == symbol]
            df_sym = df_sym[df_sym["timestamp"] <= now_ts].tail(300)
            if len(df_sym) < 50:
                continue
            try:
                mem_con.unregister("trends")
            except Exception:
                pass
            mem_con.register("trends", df_sym)
            ana = analyze_trends(symbol, connection=mem_con)
            if ana:
                analyses[symbol] = ana
                current_prices[symbol] = ana["current_price"]

        # Exit / stop-loss first
        for symbol in list(positions.keys()):
            if symbol not in current_prices:
                continue
            ana = analyses.get(symbol) or {}
            price = current_prices[symbol]
            pos = positions[symbol]
            entry = pos["entry_price"]
            qty = pos["qty"]

            stop_hit = entry > 0 and price <= entry * (1 - STOP_LOSS_PCT)
            exit_signal = (
                ana.get("near_lower_band")
                or ana.get("sar_above_price")
                or ana.get("sar_flipped_to_bear")
                or ana.get("dive_bombing")
                or ana.get("bearish_crossover")
            )
            if stop_hit or exit_signal:
                cash += qty * price
                pnl = (price - entry) * qty
                trades.append({"symbol": symbol, "side": "SELL", "price": price, "qty": qty, "pnl": pnl, "ts": now_ts})
                log_con.execute(
                    f"INSERT INTO {TRADE_LOG_TABLE} (timestamp_utc, symbol, side) VALUES (?, ?, ?)",
                    [now_ts, symbol, "SELL"],
                )
                del positions[symbol]

        # Then entries (same rules as trading.py)
        daily_count = _count_daily_sim(log_con, now_ts)
        weekly_count = _count_weekly_sim(log_con, now_ts)
        open_count = len(positions)

        for symbol in symbols:
            ana = analyses.get(symbol)
            if not ana or not ana.get("strong_trend"):
                continue
            if daily_count >= MAX_DAILY_TRADES or weekly_count >= MAX_WEEKLY_TRADES:
                break
            if symbol in positions:
                continue
            if open_count >= MAX_OPEN_POSITIONS:
                break

            bullish_trigger = (
                ana.get("bullish_crossover")
                or ana.get("sar_flipped_to_bull")
                or ana.get("bullish_crossover_recent", False)
                or ana.get("sar_flipped_to_bull_recent", False)
            )
            buy = (
                ana.get("trending_up_a_lot")
                and ana.get("near_upper_band")
                and ana.get("sar_below_price")
                and bullish_trigger
                and not ana.get("similar_to_yesterday", False)
                and not ana.get("bb_squeeze", False)
                and not ana.get("avoid_long", False)
            )
            if buy:
                price = current_prices[symbol]
                qty = 1  # fixed for backtest; position sizing is separate
                cost = qty * price
                if cost > cash:
                    continue
                cash -= cost
                positions[symbol] = {"qty": qty, "entry_price": price}
                trades.append({"symbol": symbol, "side": "BUY", "price": price, "qty": qty, "pnl": None, "ts": now_ts})
                log_con.execute(
                    f"INSERT INTO {TRADE_LOG_TABLE} (timestamp_utc, symbol, side) VALUES (?, ?, ?)",
                    [now_ts, symbol, "BUY"],
                )
                daily_count += 1
                weekly_count += 1
                open_count += 1

        # Equity at this step
        equity = cash
        for sym, pos in positions.items():
            equity += pos["qty"] * current_prices.get(sym, pos["entry_price"])
        equity_curve.append((now_ts, equity))
        if equity > peak_equity:
            peak_equity = equity
        if peak_equity > 0:
            dd = (peak_equity - equity) / peak_equity * 100
            if dd > max_drawdown_pct:
                max_drawdown_pct = dd

    mem_con.close()
    log_con.close()
    con.close()

    final_equity = cash + sum(pos["qty"] * pos["entry_price"] for pos in positions.values())
    total_return_pct = (final_equity - initial_capital) / initial_capital * 100 if initial_capital else 0.0
    closed_trades = [t for t in trades if t["side"] == "SELL"]
    wins = sum(1 for t in closed_trades if t["pnl"] and t["pnl"] > 0)
    win_rate = wins / len(closed_trades) * 100 if closed_trades else 0.0

    if equity_curve_path:
        with open(equity_curve_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "equity"])
            w.writerows(equity_curve)

    return {
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "num_trades": len(trades),
        "win_rate": win_rate,
        "final_equity": final_equity,
        "trades": trades,
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest strategy on historical data")
    parser.add_argument("--symbols", type=str, default="AAPL,MSFT", help="Comma-separated symbols")
    parser.add_argument("--start", type=str, required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=100_000.0, help="Initial capital")
    parser.add_argument("--table", type=str, default=DEFAULT_TABLE, help="DuckDB table with candles")
    parser.add_argument("--equity-curve", type=str, default=None, help="Optional CSV path for equity curve")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    try:
        result = run_backtest(
            symbols,
            args.start,
            args.end,
            initial_capital=args.capital,
            table=args.table,
            equity_curve_path=args.equity_curve,
        )
    except Exception as e:
        logger.error(e)
        return 1

    print("Backtest results")
    print("---------------")
    print(f"Total return:    {result['total_return_pct']:.2f}%")
    print(f"Max drawdown:    {result['max_drawdown_pct']:.2f}%")
    print(f"Number of trades: {result['num_trades']}")
    print(f"Win rate:        {result['win_rate']:.1f}%")
    print(f"Final equity:    ${result['final_equity']:,.2f}")
    if args.equity_curve:
        print(f"Equity curve:    {args.equity_curve}")

    return 0


if __name__ == "__main__":
    exit(main())
