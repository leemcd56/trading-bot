# trading.py
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from numbers import Number
from urllib.parse import urlparse
from uuid import UUID
import pytz
import duckdb
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv
from utils import logger
from alerts import send_alert
from data_providers import get_intraday_price
import ai_review
from ai_review import confirm_trade
from config import (
    SYMBOLS,
    MAX_DAILY_TRADES,
    MAX_WEEKLY_TRADES,
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_RISK_PCT,
    MAX_POSITION_CORRELATION,
    CORRELATION_LOOKBACK_DAYS,
    CORRELATION_MIN_SAMPLES,
    MAX_SLIPPAGE_PCT,
    CIRCUIT_BREAKER_ENABLED,
    CIRCUIT_BREAKER_DRAWDOWN_PCT,
    STOP_LOSS_PCT,
    TRADE_LOG_RETAIN_DAYS,
    DB_PATH,
    RISK_PCT_PER_TRADE,
    MAX_POSITION_PCT_EQUITY,
    MIN_SHARES,
    MAX_SHARES,
    NOTIONAL_PER_TRADE,
    MAX_DAY_TRADES_IN_5_DAYS,
    TRAIL_ACTIVATION_PCT,
    TRAIL_PCT,
    REQUIRE_BULLISH_TRIGGER,
    REQUIRE_NEAR_UPPER_BAND,
    REQUIRE_ADX_RISING,
    REQUIRE_VOLUME_CONFIRMATION,
    LONG_TERM_SMA_PERIOD,
    MIN_HOLD_HOURS,
    TRADING_MODE,
    MEAN_REVERSION_ENABLED,
    MEAN_REVERSION_ADX_CEILING,
    MEAN_REVERSION_RSI_OVERSOLD,
    MEAN_REVERSION_RSI_OVERBOUGHT,
    MEAN_REVERSION_STOP_LOSS_PCT,
    MEAN_REVERSION_RISK_PCT_PER_TRADE,
)

load_dotenv()

_base_url = os.getenv("ALPACA_BASE_URL")
# Strip trailing /v2 — the SDK appends it internally, so including it in
# url_override results in double /v2 paths (e.g. /v2/v2/account → 404).
if _base_url:
    _base_url = _base_url.rstrip("/")
    if _base_url.endswith("/v2"):
        _base_url = _base_url[:-3]
_PAPER_TRADING_ENABLED = True
trading_client = TradingClient(
    api_key=os.getenv('ALPACA_API_KEY'),
    secret_key=os.getenv('ALPACA_SECRET_KEY'),
    paper=_PAPER_TRADING_ENABLED,   # Change to False only when going live (very carefully!)
    url_override=_base_url if _base_url else None,
)

# "source" tag recorded in trade_history for a mean-reversion entry, so a
# later exit check can tell which strategy opened the position (see
# _last_buy_source / _try_risk_exit / _try_mean_reversion).
MEAN_REVERSION_SOURCE = "mean_reversion"

# IRS wash-sale rule: a loss is disallowed if the same (or substantially
# identical) security is bought within 30 days before or after the loss sale.
# See _wash_sale_risk for which direction this bot actually checks.
WASH_SALE_WINDOW_DAYS = 30

TRADE_LOG_TABLE = "trade_log"
TRAIL_STATE_TABLE = "trail_state"
TRADE_HISTORY_TABLE = "trade_history"
PENDING_ORDERS_TABLE = "pending_orders"
# Same table report.snapshot_portfolio() writes "open"/"close" equity into.
# Duplicated here (schema + table name) rather than imported, since report.py
# already imports from trading.py - importing back would be circular. This
# mirrors the existing _ensure_trade_log/_ensure_trade_history duplication
# between the two modules.
PORTFOLIO_SNAPSHOTS_TABLE = "portfolio_snapshots"
CIRCUIT_BREAKER_TRIPS_TABLE = "circuit_breaker_trips"


def _ensure_trade_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRADE_LOG_TABLE} (
            timestamp_utc DOUBLE,
            symbol VARCHAR,
            side VARCHAR,
            qty DOUBLE
        )
    """)
    # Migration: add qty if table existed without it
    try:
        con.execute(f"ALTER TABLE {TRADE_LOG_TABLE} ADD COLUMN qty DOUBLE")
    except Exception:
        pass  # column already exists


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


def _ensure_portfolio_snapshots(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {PORTFOLIO_SNAPSHOTS_TABLE} (
            timestamp_utc DOUBLE,
            date_et VARCHAR,
            label VARCHAR,
            equity DOUBLE
        )
    """)


def _ensure_circuit_breaker_trips(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {CIRCUIT_BREAKER_TRIPS_TABLE} (
            date_et VARCHAR PRIMARY KEY,
            tripped_at DOUBLE
        )
    """)


def _ensure_pending_orders(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {PENDING_ORDERS_TABLE} (
            order_id VARCHAR PRIMARY KEY,
            symbol VARCHAR,
            side VARCHAR,
            source VARCHAR,
            fallback_price DOUBLE,
            recorded_qty DOUBLE,
            submitted_at DOUBLE
        )
    """)


def _to_float(value) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, Number):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return float(stripped)
        return None
    except (TypeError, ValueError):
        return None


def _enum_value(value):
    return getattr(value, "value", value)


def _order_status(order) -> str:
    status = _enum_value(getattr(order, "status", None))
    return str(status).lower().strip() if status is not None else ""


def _order_id(order) -> str | None:
    oid = getattr(order, "id", None)
    if isinstance(oid, UUID):
        return str(oid)
    if isinstance(oid, str) and oid.strip():
        return oid.strip()
    return None


def _order_filled_qty(order) -> float:
    filled_qty = _to_float(getattr(order, "filled_qty", None))
    if filled_qty is not None:
        return max(0.0, filled_qty)
    if _order_status(order) == "filled":
        requested_qty = _to_float(getattr(order, "qty", None))
        if requested_qty is not None:
            return max(0.0, requested_qty)
    return 0.0


def _order_fill_price(order, fallback_price: float | None) -> float | None:
    return _to_float(getattr(order, "filled_avg_price", None)) or fallback_price


def _alpaca_host_mode(url: str | None) -> str | None:
    if not url:
        return "paper" if _PAPER_TRADING_ENABLED else "live"
    host = urlparse(url).netloc.lower()
    if "paper-api.alpaca.markets" in host:
        return "paper"
    if host == "api.alpaca.markets" or host.endswith(".api.alpaca.markets"):
        return "live"
    return None


def get_trading_runtime_status() -> dict:
    """
    Report whether order submission is currently allowed.
    This bot must remain in paper mode; a live-host override is a hard stop.
    """
    if TRADING_MODE == "dormant":
        return {
            "orders_allowed": False,
            "state": "dormant",
            "label": "Dormant",
            "detail": "Dormant mode disables all order submission.",
        }

    host_mode = _alpaca_host_mode(_base_url)
    if host_mode and host_mode != ("paper" if _PAPER_TRADING_ENABLED else "live"):
        return {
            "orders_allowed": False,
            "state": "misconfigured",
            "label": "Misconfigured",
            "detail": (
                "Paper trading is enabled, but ALPACA_BASE_URL points at a live Alpaca host."
                if _PAPER_TRADING_ENABLED
                else "Live trading is enabled, but ALPACA_BASE_URL points at the paper host."
            ),
        }

    return {
        "orders_allowed": True,
        "state": "paper",
        "label": "Paper",
        "detail": "Paper trading is enabled.",
    }


def _orders_allowed() -> tuple[bool, str]:
    status = get_trading_runtime_status()
    return bool(status["orders_allowed"]), str(status["detail"])


def _remember_pending_order(
    order_id: str,
    symbol: str,
    side: str,
    source: str,
    fallback_price: float | None,
) -> None:
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_pending_orders(con)
        con.execute(f"DELETE FROM {PENDING_ORDERS_TABLE} WHERE order_id = ?", [order_id])
        con.execute(
            f"""
            INSERT INTO {PENDING_ORDERS_TABLE}
                (order_id, symbol, side, source, fallback_price, recorded_qty, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [order_id, symbol, side, source, fallback_price, 0.0, time.time()],
        )
    finally:
        con.close()


def _log_fill_slippage(symbol: str, side: str, source: str, fallback_price: float | None, fill_price: float) -> None:
    """
    Compare the actual fill against the price the decision was made on.
    Only meaningful for the notional/fractional order path, which Alpaca only
    supports as plain market orders - qty-based orders are already capped at
    MAX_SLIPPAGE_PCT by _build_qty_order's marketable limit price, so this is
    the only place slippage can exceed the cap.
    """
    if not fallback_price or fallback_price <= 0:
        return
    raw_pct = (fill_price - fallback_price) / fallback_price
    # Positive = adverse (paid more on a BUY, received less on a SELL).
    # Negative = favorable fill - never worth a warning, however large.
    adverse_pct = raw_pct if side.upper() == "BUY" else -raw_pct
    if adverse_pct < MAX_SLIPPAGE_PCT:
        return
    msg = (
        f"{symbol} [{source}]: {side} filled at ${fill_price:.2f} vs expected ${fallback_price:.2f} "
        f"({adverse_pct:+.2%} adverse slippage, cap is {MAX_SLIPPAGE_PCT:.1%})"
    )
    logger.warning(msg)
    send_alert(msg, "error")


def _fifo_cost_basis(symbol: str, before_ts: float, qty_needed: float) -> float | None:
    """
    Average cost per share for the oldest `qty_needed` currently-open shares
    of `symbol`, replaying trade_history in timestamp order (FIFO) up to (but
    not including) `before_ts`. trade_history is never pruned, so this has
    the bot's full history for the symbol to work with.

    Returns None if there isn't enough open BUY history to cover
    `qty_needed` - e.g. a position opened before the bot started recording,
    or opened manually in Alpaca. A wash-sale check with no real cost basis
    would just be a guess, so callers must skip rather than flag in that case.
    """
    con = None
    try:
        con = duckdb.connect(DB_PATH)
        _ensure_trade_history(con)
        rows = con.execute(
            f"SELECT side, qty, price FROM {TRADE_HISTORY_TABLE} "
            "WHERE symbol = ? AND timestamp_utc < ? ORDER BY timestamp_utc ASC",
            [symbol, before_ts],
        ).fetchall()
    except Exception as e:
        logger.warning(f"Could not read trade history for FIFO cost basis on {symbol}: {e}")
        return None
    finally:
        if con is not None:
            con.close()

    open_lots: list[list[float]] = []  # [[qty, price], ...] oldest first
    for side, qty, price in rows:
        qty = _to_float(qty) or 0.0
        price = _to_float(price) or 0.0
        if qty <= 0:
            continue
        if side == "BUY":
            open_lots.append([qty, price])
        elif side == "SELL":
            remaining = qty
            while remaining > 1e-9 and open_lots:
                lot = open_lots[0]
                take = min(lot[0], remaining)
                lot[0] -= take
                remaining -= take
                if lot[0] <= 1e-9:
                    open_lots.pop(0)

    remaining_needed = qty_needed
    total_cost = 0.0
    for lot_qty, lot_price in open_lots:
        if remaining_needed <= 1e-9:
            break
        take = min(lot_qty, remaining_needed)
        total_cost += take * lot_price
        remaining_needed -= take

    if remaining_needed > 1e-6:
        return None
    return total_cost / qty_needed


def _wash_sale_risk(symbol: str, buy_ts: float) -> dict | None:
    """
    Checks whether a BUY happening now would trigger a wash sale: the IRS
    disallows a loss if you buy the same security back within 30 days of
    selling it at a loss. This only checks that one direction - "did I just
    sell this at a loss, and am I now buying it back" - not a prior buy
    shortly before an upcoming loss sale. The latter would require guessing
    which shares are "replacement" shares at the moment they're bought,
    before we know a later sale will even be at a loss; this direction is
    unambiguous because the position is flat (fully closed) at the loss
    sale, so any later buy is clearly a new, separate purchase. This is a
    lightweight heuristic flag for awareness, not tax advice - it never
    blocks or alters the BUY that already happened.

    Returns a dict describing the prior loss sale if one is found within
    WASH_SALE_WINDOW_DAYS, else None (no recent loss sale, the recent sale
    wasn't a loss, or there isn't enough trade history to tell).
    """
    con = None
    try:
        con = duckdb.connect(DB_PATH)
        _ensure_trade_history(con)
        window_start = buy_ts - WASH_SALE_WINDOW_DAYS * 86400
        row = con.execute(
            f"SELECT timestamp_utc, qty, price FROM {TRADE_HISTORY_TABLE} "
            "WHERE symbol = ? AND side = 'SELL' AND timestamp_utc >= ? AND timestamp_utc < ? "
            "ORDER BY timestamp_utc DESC LIMIT 1",
            [symbol, window_start, buy_ts],
        ).fetchone()
    except Exception as e:
        logger.warning(f"Could not check wash-sale risk for {symbol}: {e}")
        return None
    finally:
        if con is not None:
            con.close()

    if not row:
        return None
    sell_ts, sell_qty, sell_price = row
    sell_qty = _to_float(sell_qty) or 0.0
    sell_price = _to_float(sell_price) or 0.0
    if sell_qty <= 0 or sell_price <= 0:
        return None

    cost_basis = _fifo_cost_basis(symbol, sell_ts, sell_qty)
    if cost_basis is None or sell_price >= cost_basis:
        return None

    return {
        "sell_date_et": datetime.fromtimestamp(sell_ts, tz=_ET).strftime("%Y-%m-%d"),
        "sell_price": sell_price,
        "cost_basis": cost_basis,
        "loss_amount": (cost_basis - sell_price) * sell_qty,
    }


def _check_wash_sale_risk(symbol: str, buy_ts: float) -> None:
    """
    Informational only - called after a BUY has already filled, never blocks
    or delays a trade. Logs + alerts once per matching loss sale found, so a
    held position that triggers no further buys doesn't generate noise.
    """
    risk = _wash_sale_risk(symbol, buy_ts)
    if risk is None:
        return
    msg = (
        f"⚠️ Possible wash sale: {symbol} bought back within {WASH_SALE_WINDOW_DAYS} days of a "
        f"${risk['loss_amount']:,.2f} loss sale on {risk['sell_date_et']} (sold @ ${risk['sell_price']:.2f}, "
        f"cost basis ${risk['cost_basis']:.2f}). That loss may be disallowed for this tax year - "
        f"it gets added to this new position's cost basis instead. Not tax advice; consult a professional."
    )
    logger.warning(msg)
    send_alert(msg, "error")


def _record_order_fill_delta(
    symbol: str,
    side: str,
    source: str,
    fallback_price: float | None,
    order,
    recorded_qty: float,
) -> float:
    filled_qty = _order_filled_qty(order)
    delta = max(0.0, filled_qty - recorded_qty)
    if delta <= 0:
        return 0.0
    actual_fill_price = _to_float(getattr(order, "filled_avg_price", None))
    if actual_fill_price is not None:
        _log_fill_slippage(symbol, side, source, fallback_price, actual_fill_price)
    _record_trade(symbol, side, delta)
    _record_trade_history(symbol, side, delta, actual_fill_price or fallback_price, source)
    if side.upper() == "BUY":
        _check_wash_sale_risk(symbol, time.time())
    return delta


def reconcile_pending_orders(
    order_id: str | None = None,
    attempts: int = 1,
) -> dict[str, float]:
    """
    Reconcile previously submitted orders against Alpaca.
    Only filled quantities are written into trade_log/trade_history, so unfilled
    orders never consume PDT or trade-cap slots locally.
    """
    if not hasattr(trading_client, "get_order_by_id"):
        return {}

    attempts = max(1, int(attempts))
    recorded: dict[str, float] = {}
    terminal_statuses = {"filled", "canceled", "cancelled", "expired", "rejected"}

    for _ in range(attempts):
        con = duckdb.connect(DB_PATH)
        try:
            _ensure_pending_orders(con)
            if order_id:
                rows = con.execute(
                    f"""
                    SELECT order_id, symbol, side, source, fallback_price, recorded_qty
                    FROM {PENDING_ORDERS_TABLE}
                    WHERE order_id = ?
                    """,
                    [order_id],
                ).fetchall()
            else:
                rows = con.execute(
                    f"""
                    SELECT order_id, symbol, side, source, fallback_price, recorded_qty
                    FROM {PENDING_ORDERS_TABLE}
                    ORDER BY submitted_at
                    """
                ).fetchall()
        finally:
            con.close()

        if not rows:
            break

        for row_order_id, symbol, side, source, fallback_price, recorded_qty in rows:
            try:
                remote_order = trading_client.get_order_by_id(row_order_id)
            except Exception as err:
                logger.warning(f"Could not reconcile order {row_order_id} for {symbol}: {err}")
                continue

            prior_recorded = float(recorded_qty or 0)
            new_delta = _record_order_fill_delta(
                symbol=symbol,
                side=side,
                source=source,
                fallback_price=fallback_price,
                order=remote_order,
                recorded_qty=prior_recorded,
            )
            if new_delta > 0:
                recorded[row_order_id] = recorded.get(row_order_id, 0.0) + new_delta

            latest_recorded = prior_recorded + new_delta
            status = _order_status(remote_order)

            con = duckdb.connect(DB_PATH)
            try:
                _ensure_pending_orders(con)
                if status in terminal_statuses:
                    con.execute(f"DELETE FROM {PENDING_ORDERS_TABLE} WHERE order_id = ?", [row_order_id])
                    if side.upper() == "SELL" and latest_recorded > 0 and status == "filled":
                        _clear_trail_state(symbol)
                else:
                    con.execute(
                        f"UPDATE {PENDING_ORDERS_TABLE} SET recorded_qty = ? WHERE order_id = ?",
                        [latest_recorded, row_order_id],
                    )
            finally:
                con.close()

    return recorded


def _record_trade_history(symbol: str, side: str, qty: float, price: float | None, source: str) -> None:
    """Persist a human-readable trade record with price for EOD reporting."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_history(con)
        con.execute(
            f"INSERT INTO {TRADE_HISTORY_TABLE} (timestamp_utc, symbol, side, qty, price, source) VALUES (?, ?, ?, ?, ?, ?)",
            [time.time(), symbol, side, qty or 0, price, source],
        )
    finally:
        con.close()


def _ensure_trail_state(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRAIL_STATE_TABLE} (
            symbol VARCHAR PRIMARY KEY,
            running_high DOUBLE,
            updated_at DOUBLE
        )
    """)


def _record_trade(symbol: str, side: str, qty: float = 0) -> None:
    """Persist trade to DuckDB for daily/weekly limit counts and PDT. qty=0 for unknown (e.g. notional)."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        ts = time.time()
        con.execute(
            f"INSERT INTO {TRADE_LOG_TABLE} (timestamp_utc, symbol, side, qty) VALUES (?, ?, ?, ?)",
            [ts, symbol, side, qty if qty else 0],
        )
    finally:
        con.close()


_ET = pytz.timezone("US/Eastern")


def _today_et_start_ts() -> float:
    """Unix timestamp of midnight ET today."""
    now_et = datetime.now(_ET)
    midnight_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_et.timestamp()


def _count_daily() -> int | None:
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        out = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ? AND side = 'BUY'",
            [_today_et_start_ts()],
        ).fetchone()
        return out[0] if out else 0
    except Exception as e:
        logger.warning(f"Could not read trade log for daily count: {e}")
        return None
    finally:
        con.close()


def _count_weekly() -> int | None:
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        now = time.time()
        week_ago = now - 7 * 86400
        out = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ? AND side = 'BUY'",
            [week_ago],
        ).fetchone()
        return out[0] if out else 0
    except Exception as e:
        logger.warning(f"Could not read trade log for weekly count: {e}")
        return None
    finally:
        con.close()


def _count_day_trades_in_last_5_days() -> int:
    """
    Count day trades in the rolling past 5 calendar days (UTC).
    A day trade = a SELL that closes shares bought the same day.
    Uses qty when present; treats 0/NULL as 1 for conservative count.
    """
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        cutoff = time.time() - 5 * 86400
        rows = con.execute(
            f"SELECT timestamp_utc, symbol, side, qty FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ? ORDER BY timestamp_utc",
            [cutoff],
        ).fetchall()
    finally:
        con.close()
    # Group by (day_id, symbol); day_id = UTC day
    groups = defaultdict(list)
    for ts, sym, side, qty in rows:
        day_id = int(ts // 86400)
        q = max(1, float(qty or 0)) if qty is not None else 1
        groups[(day_id, sym)].append((ts, side, q))
    total_day_trades = 0
    for key, events in groups.items():
        events.sort(key=lambda x: x[0])
        same_day_bought = 0.0
        for _ts, side, q in events:
            if side.upper() == "BUY":
                same_day_bought += q
            else:
                close_qty = min(q, same_day_bought)
                same_day_bought -= close_qty
                if close_qty > 0:
                    total_day_trades += 1
    return total_day_trades


def _would_sell_be_day_trade(symbol: str) -> bool:
    """True if we have any BUY of this symbol today (UTC); selling would then be a day trade."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        now = time.time()
        today_start = (int(now) // 86400) * 86400
        out = con.execute(
            f"SELECT 1 FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ? AND timestamp_utc < ? AND symbol = ? AND side = 'BUY' LIMIT 1",
            [today_start, today_start + 86400, symbol],
        ).fetchone()
        return out is not None
    finally:
        con.close()


def _should_block_sell_pdt(symbol: str) -> bool:
    """True if we should block this SELL to avoid exceeding PDT limit (day trade count in 5 days)."""
    if MAX_DAY_TRADES_IN_5_DAYS is None or MAX_DAY_TRADES_IN_5_DAYS < 0:
        return False
    if not _would_sell_be_day_trade(symbol):
        return False
    return _count_day_trades_in_last_5_days() >= MAX_DAY_TRADES_IN_5_DAYS


def prune_old_trade_log() -> None:
    """Delete trade_log rows older than TRADE_LOG_RETAIN_DAYS (daily/weekly counts need 7+ days)."""
    if TRADE_LOG_RETAIN_DAYS <= 0:
        return
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        cutoff = time.time() - TRADE_LOG_RETAIN_DAYS * 86400
        con.execute(f"DELETE FROM {TRADE_LOG_TABLE} WHERE timestamp_utc < ?", [cutoff])
        logger.debug(f"Pruned trade_log older than {TRADE_LOG_RETAIN_DAYS} days")
    except Exception as e:
        logger.warning(f"Prune trade_log failed: {e}")
    finally:
        con.close()


def _get_trail_running_high(symbol: str) -> float | None:
    """Return persisted running high for symbol, or None if not set."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trail_state(con)
        out = con.execute(
            f"SELECT running_high FROM {TRAIL_STATE_TABLE} WHERE symbol = ?",
            [symbol],
        ).fetchone()
        return float(out[0]) if out and out[0] is not None else None
    finally:
        con.close()


def _set_trail_running_high(symbol: str, running_high: float) -> None:
    """Upsert running high for symbol (trailing stop state)."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trail_state(con)
        now = time.time()
        con.execute(
            f"DELETE FROM {TRAIL_STATE_TABLE} WHERE symbol = ?",
            [symbol],
        )
        con.execute(
            f"INSERT INTO {TRAIL_STATE_TABLE} (symbol, running_high, updated_at) VALUES (?, ?, ?)",
            [symbol, running_high, now],
        )
    finally:
        con.close()


def _clear_trail_state(symbol: str) -> None:
    """Clear trailing-stop state for symbol after we sell."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trail_state(con)
        con.execute(f"DELETE FROM {TRAIL_STATE_TABLE} WHERE symbol = ?", [symbol])
    except Exception as e:
        logger.debug(f"Clear trail state failed for {symbol}: {e}")
    finally:
        con.close()


def _fetch_positions() -> list | None:
    """
    Single place that calls trading_client.get_all_positions(), so a caller
    that needs more than one view of the position list (count, symbols, risk)
    can fetch once and pass the same list around instead of each view hitting
    the API separately. Returns None on failure.
    """
    try:
        return trading_client.get_all_positions()
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return None


def _open_positions_count(positions: list | None = None) -> int | None:
    if positions is None:
        positions = _fetch_positions()
    if positions is None:
        return None
    try:
        return sum(1 for p in positions if float(p.qty) > 0)
    except Exception as e:
        logger.error(f"Failed to parse positions: {e}")
        return None


def get_open_position_symbols(positions: list | None = None) -> set[str] | None:
    """Return held symbols with positive quantity, or None on API/parse failure."""
    if positions is None:
        positions = _fetch_positions()
    if positions is None:
        return None
    try:
        return {
            str(p.symbol).upper()
            for p in positions
            if _to_float(getattr(p, "qty", 0)) and float(p.qty) > 0
        }
    except Exception as e:
        logger.error(f"Failed to parse positions: {e}")
        return None


def _portfolio_open_risk_pct(equity: float, positions: list | None = None) -> float | None:
    """
    Fraction of equity that would be lost if every currently open position
    hit its stop-loss right now: sum(qty * avg_entry_price * STOP_LOSS_PCT) / equity.
    Uses the same stop definition as the actual exit check in _try_risk_exit
    (entry * (1 - STOP_LOSS_PCT)), not the ATR-widened distance used to size a
    single new trade, so this reflects real aggregate downside across all
    held symbols - the thing MAX_OPEN_POSITIONS (a plain headcount) can't see.
    Returns None if positions can't be read or parsed; caller should fail closed.
    """
    if equity <= 0:
        return None
    if positions is None:
        positions = _fetch_positions()
    if positions is None:
        return None
    try:
        total_risk = 0.0
        for p in positions:
            qty = _to_float(getattr(p, "qty", 0)) or 0.0
            entry = _to_float(getattr(p, "avg_entry_price", 0)) or 0.0
            if qty > 0 and entry > 0:
                total_risk += qty * entry * STOP_LOSS_PCT
        return total_risk / equity
    except Exception as e:
        logger.error(f"Failed to parse positions for portfolio risk check: {e}")
        return None


def _daily_returns(symbol: str, lookback_days: int) -> dict[int, float] | None:
    """
    timestamp -> daily return for `symbol`'s last `lookback_days` bars in the
    `trends` table. Returns None on a DB read failure; returns {} (not None)
    when the symbol simply has no/too-little history yet (e.g. never backfilled).
    """
    con = None
    try:
        con = duckdb.connect(DB_PATH)
        rows = con.execute(
            """
            SELECT timestamp, close FROM trends
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            [symbol, lookback_days + 1],
        ).fetchall()
    except Exception as e:
        logger.error(f"Correlation check: failed to read history for {symbol}: {e}")
        return None
    finally:
        if con is not None:
            con.close()

    bars = sorted(
        ((int(ts), float(close)) for ts, close in rows if close is not None and float(close) > 0),
        key=lambda bar: bar[0],
    )
    returns: dict[int, float] = {}
    for (ts_prev, close_prev), (ts, close) in zip(bars, bars[1:]):
        returns[ts] = close / close_prev - 1
    return returns


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return cov / (var_x * var_y) ** 0.5


def _correlation(
    symbol_a: str, symbol_b: str, lookback_days: int, returns_a: dict[int, float] | None = None
) -> float | None:
    """
    Pearson correlation of daily returns between two symbols, or None if
    unknown. `returns_a` lets a caller comparing one symbol against several
    others (see _max_correlation_with_held) pass in symbol_a's return series
    once instead of this function re-fetching it from DuckDB on every call.
    """
    ret_a = returns_a if returns_a is not None else _daily_returns(symbol_a, lookback_days)
    ret_b = _daily_returns(symbol_b, lookback_days)
    if ret_a is None or ret_b is None or not ret_a or not ret_b:
        return None
    shared_ts = sorted(set(ret_a) & set(ret_b))
    if len(shared_ts) < CORRELATION_MIN_SAMPLES:
        return None
    return _pearson_correlation([ret_a[t] for t in shared_ts], [ret_b[t] for t in shared_ts])


def _max_correlation_with_held(symbol: str, held_symbols: set[str], lookback_days: int) -> float | None:
    """
    Highest |correlation| between `symbol` and any currently held symbol.
    Best-effort/fail-open by design: a pair with missing or too-little shared
    history is simply excluded rather than blocking the trade. This is a
    diversification check, not a hard safety rail - those are the daily/
    weekly/open-position/portfolio-heat caps in _entry_caps_allow_buy, which
    already fail closed on real DB/API problems before this check even runs.
    Returns None if no pair had enough data to judge.
    """
    # symbol's own return history doesn't change per held symbol - fetch it
    # once rather than once per comparison (held_symbols can be several names).
    returns_for_symbol = _daily_returns(symbol, lookback_days)
    best = None
    for held in held_symbols:
        if held.upper() == symbol.upper():
            continue
        corr = _correlation(symbol, held, lookback_days, returns_a=returns_for_symbol)
        if corr is None:
            continue
        if best is None or abs(corr) > abs(best):
            best = corr
    return best


def get_locally_known_held_symbols() -> set[str] | None:
    """
    Derive held symbols from the locally recorded filled trade log.
    Used only as a fallback when the live positions read fails.
    """
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        rows = con.execute(
            f"""
            SELECT
                symbol,
                SUM(
                    CASE
                        WHEN UPPER(side) = 'BUY' THEN CASE WHEN qty IS NULL OR qty <= 0 THEN 1 ELSE qty END
                        WHEN UPPER(side) = 'SELL' THEN -CASE WHEN qty IS NULL OR qty <= 0 THEN 1 ELSE qty END
                        ELSE 0
                    END
                ) AS net_qty
            FROM {TRADE_LOG_TABLE}
            GROUP BY symbol
            HAVING net_qty > 0
            """
        ).fetchall()
        return {str(symbol).upper() for symbol, _net_qty in rows if symbol}
    except Exception as e:
        logger.error(f"Failed to derive locally known held symbols: {e}")
        return None
    finally:
        con.close()


def _get_account_equity() -> float | None:
    """Return current account equity (portfolio value). Returns None on error."""
    try:
        account = trading_client.get_account()
        # Alpaca returns equity as string
        return float(account.equity or 0)
    except Exception as e:
        logger.warning(f"Failed to get account equity: {e}")
        return None


def _get_buying_power() -> float:
    """Return current buying power. Returns 0 on error."""
    try:
        account = trading_client.get_account()
        return float(account.buying_power or 0)
    except Exception as e:
        logger.warning(f"Failed to get buying power: {e}")
        return 0.0


def _existing_position_value(symbol: str, price: float) -> float:
    """
    Current market value of any existing position in `symbol` (qty * price),
    or 0.0 if there isn't one / the lookup fails. "Fails" here overwhelmingly
    means Alpaca's normal "position does not exist" response for a symbol
    we don't hold - the common case - so this treats any lookup problem as
    "assume flat" rather than blocking the trade; see _risk_based_qty for why
    this value matters (capping TOTAL exposure to one symbol, not just the
    size of a single order).
    """
    try:
        position = trading_client.get_open_position(symbol)
        qty = _to_float(getattr(position, "qty", 0)) or 0.0
        return qty * price if qty > 0 else 0.0
    except Exception:
        return 0.0


def _risk_based_qty(
    equity: float, price: float, risk_pct: float, stop_distance: float, existing_position_value: float = 0.0
) -> int:
    """
    Shared core of risk-based position sizing, used by both the trend
    strategy (_compute_buy_qty) and the mean-reversion counter-strategy
    (_compute_mean_reversion_qty) - they differ only in which risk_pct they
    risk and how stop_distance is derived (ATR-widened vs. a fixed stop %),
    not in the sizing/clamping math itself.

    qty = (risk_pct * equity) / stop_distance_per_share, rounded down,
    clamped to MIN/MAX_SHARES and MAX_POSITION_PCT_EQUITY of equity.

    MAX_POSITION_PCT_EQUITY caps the symbol's TOTAL exposure (existing
    position value + this new order), not just this order's own size - two
    separate buys that each individually pass a per-order cap can otherwise
    stack into an oversized concentrated position (this is exactly what
    happened in practice: two ~9.7%-of-equity buys an hour apart, each under
    a 10% per-order cap, combined into a ~19.4% position that then took a
    single stop-loss loss larger than the bot's entire realized P&L for the
    following four months). Returns 0 - not MIN_SHARES - when the symbol is
    already at or over its cap, since "buy at least one share anyway" would
    defeat the cap entirely.
    """
    if price <= 0 or equity <= 0 or stop_distance <= 0:
        return MIN_SHARES
    risk_amount = risk_pct * equity
    # position_value * (stop_distance / price) = risk_amount  =>  position_value = risk_amount * price / stop_distance
    position_value = risk_amount * price / stop_distance
    qty = int(position_value / price)
    qty = max(qty, MIN_SHARES)
    qty = min(qty, MAX_SHARES)
    return _clamp_to_position_cap(qty, price, equity, existing_position_value)


def _clamp_to_position_cap(qty: int, price: float, equity: float, existing_position_value: float) -> int:
    """
    Clamp `qty` so existing_position_value + qty*price never exceeds
    MAX_POSITION_PCT_EQUITY of equity. Shared by every qty-producing path
    (risk-based sizing and the fixed-qty-1 fallbacks below) so none of them
    can accidentally bypass the same-symbol exposure cap. Returns 0 - not a
    smaller positive floor - when there's no room left, since forcing a
    minimum buy at the cap would defeat the cap entirely.
    """
    if not MAX_POSITION_PCT_EQUITY or price <= 0:
        return qty
    room_left = MAX_POSITION_PCT_EQUITY * equity - existing_position_value
    if room_left <= 0:
        return 0
    return max(0, min(qty, int(room_left / price)))


def _compute_buy_qty(analysis: dict, equity: float, existing_position_value: float = 0.0) -> int:
    """
    Compute number of shares to buy using risk-based position sizing.
    Risk per trade = RISK_PCT_PER_TRADE * equity.
    Stop distance per share = max(ATR_14, current_price * STOP_LOSS_PCT).
    If RISK_PCT_PER_TRADE is None or equity/analysis invalid, returns 1
    (still subject to the exposure cap below - a fixed-qty-1 mode gets no
    exemption from MAX_POSITION_PCT_EQUITY just because it skips risk sizing).
    `existing_position_value` is any shares of this symbol already held
    (see _risk_based_qty) so repeated buys can't stack past MAX_POSITION_PCT_EQUITY.
    """
    price = analysis.get("current_price") or 0
    if RISK_PCT_PER_TRADE is None or RISK_PCT_PER_TRADE <= 0:
        if price <= 0 or equity <= 0:
            return 1
        return _clamp_to_position_cap(1, price, equity, existing_position_value)
    if price <= 0 or equity <= 0:
        return MIN_SHARES
    atr = analysis.get("atr_14")
    stop_distance = price * STOP_LOSS_PCT
    if atr is not None and atr > 0:
        stop_distance = max(stop_distance, atr)
    return _risk_based_qty(equity, price, RISK_PCT_PER_TRADE, stop_distance, existing_position_value)


def _compute_mean_reversion_qty(analysis: dict, equity: float, existing_position_value: float = 0.0) -> int:
    """
    Risk-based sizing for the mean-reversion counter-strategy, using its own
    (smaller) MEAN_REVERSION_RISK_PCT_PER_TRADE and its own (tighter, fixed)
    MEAN_REVERSION_STOP_LOSS_PCT - not ATR-widened like _compute_buy_qty,
    since mean-reversion's stop is a precise, short-term target by design,
    not a trend-following ATR-scaled stop. `existing_position_value` - see
    _risk_based_qty - is always 0 in practice here, since _try_mean_reversion
    only ever buys when flat in the symbol, but the parameter is accepted for
    symmetry with _compute_buy_qty (and still enforced below, for the same
    reason as _compute_buy_qty's fallback: no path should be exempt from the
    exposure cap).
    """
    price = analysis.get("current_price") or 0
    if MEAN_REVERSION_RISK_PCT_PER_TRADE is None or MEAN_REVERSION_RISK_PCT_PER_TRADE <= 0:
        if price <= 0 or equity <= 0:
            return MIN_SHARES
        return _clamp_to_position_cap(MIN_SHARES, price, equity, existing_position_value)
    if price <= 0 or equity <= 0:
        return MIN_SHARES
    stop_distance = price * MEAN_REVERSION_STOP_LOSS_PCT
    return _risk_based_qty(equity, price, MEAN_REVERSION_RISK_PCT_PER_TRADE, stop_distance, existing_position_value)


def _mean_reversion_regime(analysis: dict) -> bool:
    """
    True when the market looks genuinely range-bound rather than merely
    "not quite trending": ADX must be clearly below MEAN_REVERSION_ADX_CEILING
    (a dead zone below ADX_STRONG_TREND_THRESHOLD where neither strategy
    trades), and none of the avoid_long risk heuristics (dead-cat bounce,
    extended decline, volatility spike) can be firing - those mean something
    is wrong, not that the market is calmly ranging.
    """
    if analysis.get("strong_trend"):
        return False
    if analysis.get("avoid_long"):
        return False
    adx = analysis.get("adx")
    if adx is None:
        return False
    return float(adx) < MEAN_REVERSION_ADX_CEILING


def _mean_reversion_buy_signal(analysis: dict) -> bool:
    """Oversold bounce off the lower Bollinger Band."""
    rsi = analysis.get("rsi_14")
    if rsi is None:
        return False
    return float(rsi) < MEAN_REVERSION_RSI_OVERSOLD and bool(analysis.get("near_lower_band"))


def _mean_reversion_sell_signal(analysis: dict) -> bool:
    """Take profit at the mean-reversion target: back to the top of the range, or RSI overbought."""
    rsi = analysis.get("rsi_14")
    if rsi is not None and float(rsi) > MEAN_REVERSION_RSI_OVERBOUGHT:
        return True
    return bool(analysis.get("near_upper_band"))


def _last_buy_ts(symbol: str) -> float | None:
    """Unix timestamp of the most recent BUY for symbol in the trade log, or None."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        out = con.execute(
            f"SELECT MAX(timestamp_utc) FROM {TRADE_LOG_TABLE} WHERE symbol = ? AND side = 'BUY'",
            [symbol],
        ).fetchone()
        return float(out[0]) if out and out[0] is not None else None
    finally:
        con.close()


def _last_buy_source(symbol: str) -> str | None:
    """
    'source' of the most recent BUY for symbol from trade_history ('ta',
    'signal', 'mean_reversion', ...), or None if unknown/unavailable.
    Used only to pick which exit rules apply to an open position - never a
    hard safety gate - so any failure here degrades to None (trend rules)
    rather than blocking or raising.
    """
    con = None
    try:
        con = duckdb.connect(DB_PATH)
        _ensure_trade_history(con)
        out = con.execute(
            f"SELECT source FROM {TRADE_HISTORY_TABLE} WHERE symbol = ? AND side = 'BUY' "
            "ORDER BY timestamp_utc DESC LIMIT 1",
            [symbol],
        ).fetchone()
        return str(out[0]) if out and out[0] is not None else None
    except Exception as e:
        logger.warning(f"Could not look up last buy source for {symbol}: {e}")
        return None
    finally:
        if con is not None:
            con.close()


def _held_seconds(symbol: str) -> float | None:
    """Seconds since last BUY of symbol, or None if no buy record."""
    last_buy = _last_buy_ts(symbol)
    if last_buy is None:
        return None
    return time.time() - last_buy


def _should_block_sell_min_hold(symbol: str) -> bool:
    """
    True if mode min-hold has not elapsed since the last BUY.
    Stop-loss / trailing-stop should NOT use this — only discretionary TA signal exits.
    No buy record → allow (manual/legacy positions; operator may still want out).
    """
    if MIN_HOLD_HOURS is None or MIN_HOLD_HOURS <= 0:
        return False
    held = _held_seconds(symbol)
    if held is None:
        return False
    return held < float(MIN_HOLD_HOURS) * 3600


def _ta_sell_signal(analysis: dict) -> bool:
    """
    Confirmed exit for TA path. Bare `sar_above_price` alone is NOT enough —
    that flag is true whenever price is below SAR, which dumps healthy holdings
    on routine noise. Require a real bearish confirmation.
    """
    dive = bool(analysis.get("dive_bombing"))
    sar_flip_bear = bool(analysis.get("sar_flipped_to_bear"))
    near_lower = bool(analysis.get("near_lower_band"))
    bear_x = bool(analysis.get("bearish_crossover"))
    sar_above = bool(analysis.get("sar_above_price"))
    uptrend = bool(analysis.get("uptrend"))

    # Hard exits: crash or explicit SAR regime flip on the latest bar
    if dive or sar_flip_bear:
        return True
    # Band support broken only with directional confirmation
    if near_lower and (sar_above or not uptrend):
        return True
    # DI bearish cross only with SAR or downtrend agreement
    if bear_x and (sar_above or not uptrend):
        return True
    # SAR bearish regime only when DI also agrees the uptrend is gone
    if sar_above and not uptrend:
        return True
    return False


def _skip_reasons_buy(analysis: dict) -> list[str]:
    """Return list of reasons we are not buying (for logging)."""
    reasons = []
    if not analysis.get('trending_up_a_lot'):
        reasons.append("trending_up_a_lot=False")
    if not analysis.get('sar_below_price'):
        reasons.append("sar_below_price=False")
    bullish_trigger = (
        analysis.get('bullish_crossover')
        or analysis.get('sar_flipped_to_bull')
        or analysis.get('bullish_crossover_recent')
        or analysis.get('sar_flipped_to_bull_recent')
    )
    # Only treat missing trigger as a blocker when the current mode requires it
    if REQUIRE_BULLISH_TRIGGER and not bullish_trigger:
        reasons.append("no_bullish_crossover_or_sar_flip")
    if analysis.get('similar_to_yesterday'):
        reasons.append("similar_to_yesterday=True")
    if analysis.get('bb_squeeze'):
        reasons.append("bb_squeeze=True")
    if REQUIRE_NEAR_UPPER_BAND and not analysis.get('near_upper_band'):
        reasons.append("near_upper_band=False")
    if REQUIRE_ADX_RISING and not analysis.get('adx_rising'):
        reasons.append("adx_rising=False")
    if REQUIRE_VOLUME_CONFIRMATION and not analysis.get('volume_confirmed'):
        reasons.append("volume_confirmed=False")
    if LONG_TERM_SMA_PERIOD and LONG_TERM_SMA_PERIOD > 0 and not analysis.get('above_long_term_ma'):
        reasons.append("above_long_term_ma=False")
    if analysis.get('avoid_long'):
        sub = []
        if analysis.get('dead_cat_bounce'):
            sub.append("dead_cat_bounce")
        if analysis.get('extended_decline'):
            sub.append("extended_decline")
        if analysis.get('volatility_spike'):
            sub.append("volatility_spike")
        reasons.append("avoid_long=" + ",".join(sub) if sub else "avoid_long=True")
    return reasons


def _buy_gate_scorecard(analysis: dict) -> str:
    """Compact pass/fail view of buy gates for quick log scanning."""
    bullish_trigger = (
        analysis.get('bullish_crossover')
        or analysis.get('sar_flipped_to_bull')
        or analysis.get('bullish_crossover_recent')
        or analysis.get('sar_flipped_to_bull_recent')
    )
    gates = [
        ("trend", bool(analysis.get('trending_up_a_lot'))),
        ("sar", bool(analysis.get('sar_below_price'))),
    ]
    # Conditionally include gates that the current mode actually enforces
    if REQUIRE_BULLISH_TRIGGER:
        gates.append(("trigger", bool(bullish_trigger)))
    if REQUIRE_NEAR_UPPER_BAND:
        gates.append(("nearBB", bool(analysis.get('near_upper_band'))))
    # New daily compensating filters (shown only when the mode actually requires them)
    if REQUIRE_ADX_RISING:
        gates.append(("adxUp", bool(analysis.get('adx_rising'))))
    if REQUIRE_VOLUME_CONFIRMATION:
        gates.append(("volOK", bool(analysis.get('volume_confirmed'))))
    if LONG_TERM_SMA_PERIOD and LONG_TERM_SMA_PERIOD > 0:
        gates.append(("LTma", bool(analysis.get('above_long_term_ma'))))
    gates.extend([
        ("!similar", not bool(analysis.get('similar_to_yesterday', False))),
        ("!squeeze", not bool(analysis.get('bb_squeeze', False))),
        ("!avoid", not bool(analysis.get('avoid_long', False))),
    ])
    return " ".join([f"{name}={'Y' if ok else 'N'}" for name, ok in gates])


def _todays_open_equity() -> float | None:
    """
    Today's market-open equity snapshot (written by report.snapshot_portfolio
    as the "open" label), or None if it hasn't been captured yet - e.g. the
    first few minutes after the bot starts, before open_snapshot_job has run.
    """
    con = None
    try:
        con = duckdb.connect(DB_PATH)
        _ensure_portfolio_snapshots(con)
        date_et = datetime.now(_ET).strftime("%Y-%m-%d")
        row = con.execute(
            f"SELECT equity FROM {PORTFOLIO_SNAPSHOTS_TABLE} WHERE date_et = ? AND label = 'open'",
            [date_et],
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logger.warning(f"Could not read today's open-equity snapshot: {e}")
        return None
    finally:
        if con is not None:
            con.close()


def _circuit_breaker_tripped(equity: float, open_equity: float | None) -> bool:
    """
    True when today's equity has fallen CIRCUIT_BREAKER_DRAWDOWN_PCT or more
    from this morning's open snapshot - a portfolio-wide "something is
    systemically wrong" signal, distinct from any single position's
    stop-loss (a data-feed bug causing repeated bad trades, or a real crash
    gapping through several stops at once).

    Blocks NEW entries only. Existing stop-losses/trailing-stops keep running
    normally in _try_risk_exit - panic-selling into a crash is usually the
    wrong move, so this never force-closes anything.

    Fails OPEN (not tripped) when there's no open-equity baseline yet. That's
    a data gap (too early in the day), not evidence something is wrong, and
    blocking all trading until the next snapshot would be the wrong default.
    """
    if not CIRCUIT_BREAKER_ENABLED:
        return False
    if open_equity is None or open_equity <= 0:
        return False
    drawdown_pct = (open_equity - equity) / open_equity
    return drawdown_pct >= CIRCUIT_BREAKER_DRAWDOWN_PCT


def _claim_circuit_breaker_alert_for_today() -> bool:
    """
    True the first time this is called on a given ET date, False on every
    later call that same day - lets the caller alert exactly once per trip
    instead of once per cycle for as long as the breaker stays tripped.
    On a DB error, returns False (don't alert again) since the trade is
    blocked either way regardless of this function's result.
    """
    con = None
    try:
        con = duckdb.connect(DB_PATH)
        _ensure_circuit_breaker_trips(con)
        date_et = datetime.now(_ET).strftime("%Y-%m-%d")
        if con.execute(
            f"SELECT 1 FROM {CIRCUIT_BREAKER_TRIPS_TABLE} WHERE date_et = ?", [date_et]
        ).fetchone():
            return False
        con.execute(
            f"INSERT INTO {CIRCUIT_BREAKER_TRIPS_TABLE} (date_et, tripped_at) VALUES (?, ?)",
            [date_et, time.time()],
        )
        return True
    except Exception as e:
        logger.warning(f"Could not record circuit breaker trip state: {e}")
        return False
    finally:
        if con is not None:
            con.close()


def _entry_caps_allow_buy(symbol: str, source: str) -> bool:
    daily_count = _count_daily()
    if daily_count is None:
        logger.error(f"{symbol}{source}: Refusing BUY - daily trade count unavailable")
        return False
    if daily_count >= MAX_DAILY_TRADES:
        logger.warning(
            f"{symbol}{source}: Skipping BUY - daily trade cap reached "
            f"({daily_count}/{MAX_DAILY_TRADES})"
        )
        return False

    weekly_count = _count_weekly()
    if weekly_count is None:
        logger.error(f"{symbol}{source}: Refusing BUY - weekly trade count unavailable")
        return False
    if weekly_count >= MAX_WEEKLY_TRADES:
        logger.warning(
            f"{symbol}{source}: Skipping BUY - weekly trade cap reached "
            f"({weekly_count}/{MAX_WEEKLY_TRADES})"
        )
        return False

    # Fetched once and shared below (open-position count, portfolio heat, and
    # correlation all need a view of the same position list - no reason to
    # hit the broker's get_all_positions() three separate times for one gate).
    positions = _fetch_positions()
    if positions is None:
        logger.error(f"{symbol}{source}: Refusing BUY - open positions unavailable")
        return False

    open_positions = _open_positions_count(positions)
    if open_positions is None:
        logger.error(f"{symbol}{source}: Refusing BUY - open positions count unavailable")
        return False
    if open_positions >= MAX_OPEN_POSITIONS:
        logger.warning(
            f"{symbol}{source}: Skipping BUY - max open positions ({MAX_OPEN_POSITIONS})"
        )
        return False

    equity = _get_account_equity()
    if equity is None:
        logger.error(f"{symbol}{source}: Refusing BUY - account equity unavailable for portfolio risk check")
        return False

    open_equity = _todays_open_equity()
    if _circuit_breaker_tripped(equity, open_equity):
        drawdown_pct = (open_equity - equity) / open_equity
        if _claim_circuit_breaker_alert_for_today():
            send_alert(
                f"🔴 Circuit breaker tripped: equity down {drawdown_pct:.1%} from today's open "
                f"(${open_equity:,.2f} -> ${equity:,.2f}). New entries blocked for the rest of "
                f"today; existing stop-losses/trailing-stops remain active.",
                "error",
            )
        logger.warning(
            f"{symbol}{source}: Skipping BUY - circuit breaker tripped "
            f"({drawdown_pct:.1%} drawdown >= {CIRCUIT_BREAKER_DRAWDOWN_PCT:.1%} threshold)"
        )
        return False

    open_risk_pct = _portfolio_open_risk_pct(equity, positions)
    if open_risk_pct is None:
        logger.error(f"{symbol}{source}: Refusing BUY - portfolio open risk unavailable")
        return False
    if open_risk_pct >= MAX_PORTFOLIO_RISK_PCT:
        logger.warning(
            f"{symbol}{source}: Skipping BUY - portfolio heat at cap "
            f"({open_risk_pct:.1%} of equity at risk >= {MAX_PORTFOLIO_RISK_PCT:.1%} cap)"
        )
        return False

    # Correlation check is best-effort (see _max_correlation_with_held): a data
    # gap here does NOT block the trade, unlike every check above this line.
    held_symbols = get_open_position_symbols(positions)
    if held_symbols:
        max_corr = _max_correlation_with_held(symbol, held_symbols, CORRELATION_LOOKBACK_DAYS)
        if max_corr is not None and abs(max_corr) >= MAX_POSITION_CORRELATION:
            logger.warning(
                f"{symbol}{source}: Skipping BUY - too correlated with an open position "
                f"(|corr|={abs(max_corr):.2f} >= {MAX_POSITION_CORRELATION:.2f})"
            )
            return False

    return True


def _build_qty_order(symbol: str, side: OrderSide, qty: float, reference_price: float | None):
    """
    Build a marketable limit order for a qty-based entry or discretionary exit,
    capped at MAX_SLIPPAGE_PCT away from the price the decision was made on.
    A plain market order has no protection against the live price having moved
    since analysis ran; this bounds the worst case while still pricing to fill
    immediately under normal liquidity.

    Returns None if there is no usable reference price - callers must skip the
    order rather than submit one with no price anchor at all.

    Stop-loss and trailing-stop exits (_try_risk_exit) intentionally do NOT use
    this: guaranteed execution matters more than price there, and a limit order
    can fail to fill through a fast decline. Notional/fractional orders also
    can't use this - Alpaca only supports notional on market orders - so
    slippage there is caught after the fact by _log_fill_slippage instead.
    """
    if reference_price is None or reference_price <= 0:
        return None
    if side == OrderSide.BUY:
        limit_price = round(reference_price * (1 + MAX_SLIPPAGE_PCT), 2)
    else:
        limit_price = round(reference_price * (1 - MAX_SLIPPAGE_PCT), 2)
    return LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )


def _submit_order(
    *,
    symbol: str,
    order,
    side: str,
    source: str,
    fallback_price: float | None,
    failure_prefix: str,
) -> tuple[bool, bool]:
    """
    Submit an Alpaca order and reconcile fills into the local logs.
    Returns (submitted, any_fill_recorded).
    """
    orders_allowed, reason = _orders_allowed()
    if not orders_allowed:
        logger.warning(f"{symbol} [{source}]: Skipping {side} order - {reason}")
        return False, False

    try:
        submitted_order = trading_client.submit_order(order)
    except Exception as order_err:
        logger.error(f"{failure_prefix} for {symbol}: {order_err}")
        send_alert(f"{failure_prefix} for {symbol}: {order_err}", "error")
        return False, False

    order_id = _order_id(submitted_order)
    if order_id:
        _remember_pending_order(order_id, symbol, side, source, fallback_price)
        recorded = reconcile_pending_orders(order_id=order_id, attempts=2)
        return True, order_id in recorded

    # Fall back to the returned payload when the SDK object lacks an order id.
    did_record = _record_order_fill_delta(
        symbol=symbol,
        side=side,
        source=source,
        fallback_price=fallback_price,
        order=submitted_order,
        recorded_qty=0.0,
    ) > 0
    if side.upper() == "SELL" and did_record and _order_status(submitted_order) == "filled":
        _clear_trail_state(symbol)
    if not did_record:
        logger.warning(f"{symbol} [{source}]: Order submitted but not yet filled; local trade log unchanged")
    return True, did_record


def _try_risk_exit(symbol: str, analysis: dict) -> bool:
    """
    Stop-loss and trailing-stop. Runs regardless of strong_trend so protection
    is never gated on ADX. Returns True when a risk-exit trigger was handled,
    even if an order could not be submitted or has not filled yet.

    Strategy-aware via a per-source risk profile (see _RISK_PROFILE_DEFAULT /
    the mean-reversion entry below): a position opened by the mean-reversion
    counter-strategy uses its own (tighter) MEAN_REVERSION_STOP_LOSS_PCT and
    skips the trailing stop entirely - trailing stops are a "let the winner
    run" trend concept that fights mean-reversion's thesis of taking profit
    at a fixed target (handled by _try_mean_reversion's own take-profit
    check, not here). Adding a third strategy later is a new dict entry, not
    a new branch in this function.
    """
    try:
        position = trading_client.get_open_position(symbol)
        qty = float(position.qty)
        if qty <= 0:
            return False
        entry = float(position.avg_entry_price)
        current = analysis.get("current_price") or 0
        if entry <= 0 or current <= 0:
            return False

        # Built fresh each call (not a module-level constant) so it always
        # reflects the current STOP_LOSS_PCT/MEAN_REVERSION_STOP_LOSS_PCT -
        # both are live config values tests patch at runtime.
        risk_profiles = {
            MEAN_REVERSION_SOURCE: {"stop_loss_pct": MEAN_REVERSION_STOP_LOSS_PCT, "trailing": False},
        }
        default_profile = {"stop_loss_pct": STOP_LOSS_PCT, "trailing": True}
        profile = risk_profiles.get(_last_buy_source(symbol), default_profile)
        stop_loss_pct = profile["stop_loss_pct"]

        # ─── Stop-loss ───
        if current <= entry * (1 - stop_loss_pct):
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            submitted, recorded_fill = _submit_order(
                symbol=symbol,
                order=order,
                side="SELL",
                source="stop-loss",
                fallback_price=current,
                failure_prefix="Stop-loss order FAILED",
            )
            if not submitted:
                return True
            logger.warning(
                f"Stop-loss SELL for {symbol}: price {current:.2f} <= entry {entry:.2f} "
                f"* (1 - {stop_loss_pct:.0%})"
            )
            if recorded_fill:
                send_alert(
                    f"Stop-loss SELL {symbol} qty={qty:.4g} @ {current:.2f} (entry {entry:.2f})",
                    "trade",
                )
            else:
                logger.info(f"{symbol}: Stop-loss order submitted; waiting for fill before logging trade")
            return True

        if not profile["trailing"]:
            return False

        # ─── Trailing stop ───
        running_high = _get_trail_running_high(symbol)
        if running_high is None:
            running_high = current
        else:
            running_high = max(running_high, current)
        _set_trail_running_high(symbol, running_high)
        trail_active = current >= entry * (1 + TRAIL_ACTIVATION_PCT)
        if trail_active and current <= running_high * (1 - TRAIL_PCT):
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            submitted, recorded_fill = _submit_order(
                symbol=symbol,
                order=order,
                side="SELL",
                source="trailing-stop",
                fallback_price=current,
                failure_prefix="Trailing-stop order FAILED",
            )
            if not submitted:
                return True
            logger.warning(
                f"Trailing-stop SELL for {symbol}: price {current:.2f} <= running_high "
                f"{running_high:.2f} * (1 - {TRAIL_PCT:.0%})"
            )
            if recorded_fill:
                send_alert(
                    f"Trailing-stop SELL {symbol} qty={qty:.4g} @ {current:.2f} "
                    f"(running_high {running_high:.2f})",
                    "trade",
                )
            else:
                logger.info(f"{symbol}: Trailing-stop order submitted; waiting for fill before logging trade")
            return True
    except Exception as e:
        if "position does not exist" not in str(e).lower() and "not found" not in str(e).lower():
            logger.error(f"Position check failed for {symbol}: {e}")
    return False


def _confirm_or_alert_veto(symbol: str, action: str, analysis: dict) -> dict | None:
    """
    Ask the AI gate to confirm a BUY/SELL the rules engine already decided on.
    Returns the verdict dict on CONFIRM, or None (after logging + alerting)
    on VETO so callers can `if verdict is None: return`. Callers should only
    reach this once every other skip condition has already been checked, so
    the AI is never consulted on a trade that's about to be skipped anyway.
    """
    verdict = confirm_trade(symbol, action, analysis, TRADING_MODE)
    if verdict["decision"] != "CONFIRM":
        logger.info(f"{symbol}: {action} vetoed by AI review - {verdict['reasoning']}")
        send_alert(f"{symbol}: {action} vetoed by AI review - {verdict['reasoning']}", "hodl")
        return None
    return verdict


def _ai_alert_suffix(verdict: dict) -> str:
    """Append the AI gate's reasoning to a trade alert, but only when the gate
    is actually enabled - otherwise every alert would carry a meaningless
    'AI confirmation disabled' note. Reads ai_review.AI_CONFIRMATION_ENABLED
    live (rather than a value imported at module load) so this can never
    disagree with what confirm_trade() itself just checked."""
    if not ai_review.AI_CONFIRMATION_ENABLED:
        return ""
    reasoning = verdict.get("reasoning")
    return f" | AI: {reasoning}" if reasoning else ""


def _try_mean_reversion(symbol: str, analysis: dict):
    """
    Regime-switching counter-strategy: when execute_trade finds the market
    isn't trending, trade short-term RSI/band mean-reversion instead of
    sitting out every choppy stretch entirely. OFF by default per mode
    (MEAN_REVERSION_ENABLED) - returns None (today's silent skip) whenever
    it isn't actively evaluating a choppy regime, so a mode that hasn't
    opted in behaves exactly as before.

    Shares every risk gate with the trend strategy - entry caps, portfolio
    heat, correlation, slippage-capped limit orders, AI confirmation gate -
    only the entry/exit signal, stop distance, and position size differ (see
    _mean_reversion_regime / _mean_reversion_buy_signal /
    _mean_reversion_sell_signal / _compute_mean_reversion_qty). The tighter
    stop-loss is handled by _try_risk_exit (which runs before this, in
    execute_trade) via _last_buy_source - not duplicated here.

    Returns a truthy "no signal" string (like execute_trade's trend branch)
    only when it actually evaluated a choppy regime with no entry signal, so
    main.py's no-signal digest stays quiet for anyone who hasn't opted in.
    """
    if not MEAN_REVERSION_ENABLED:
        logger.info(f"{symbol}: Skipping - no strong trend")
        return None

    try:
        position = trading_client.get_open_position(symbol)
        held_qty = float(position.qty)
    except Exception as e:
        if "position does not exist" not in str(e).lower() and "not found" not in str(e).lower():
            logger.error(f"Position check failed for {symbol}: {e}")
        held_qty = 0

    if held_qty > 0:
        if _last_buy_source(symbol) != MEAN_REVERSION_SOURCE:
            # Held by the trend strategy; its own (wider) stop-loss via
            # _try_risk_exit is the only protection while the market chops.
            logger.info(f"{symbol}: Skipping - no strong trend (held by trend strategy)")
            return None
        if not _mean_reversion_sell_signal(analysis):
            logger.info(
                f"{symbol}: Holding mean-reversion position, target not hit yet "
                f"(RSI={analysis.get('rsi_14')})"
            )
            return None

        sell_price = analysis.get("current_price")
        order = _build_qty_order(symbol, OrderSide.SELL, held_qty, sell_price)
        if order is None:
            logger.warning(f"{symbol}: Skipping mean-reversion SELL - no usable reference price for limit order")
            return None
        verdict = _confirm_or_alert_veto(symbol, "SELL", analysis)
        if verdict is None:
            return None
        submitted, recorded_fill = _submit_order(
            symbol=symbol,
            order=order,
            side="SELL",
            source=MEAN_REVERSION_SOURCE,
            fallback_price=sell_price,
            failure_prefix="Mean-reversion SELL order FAILED",
        )
        if not submitted:
            return None
        logger.info(f"Mean-reversion SELL submitted for {symbol} limit=${order.limit_price:.2f}")
        if recorded_fill:
            send_alert(f"Mean-reversion SELL {symbol} qty={held_qty:.4g}{_ai_alert_suffix(verdict)}", "trade")
        return None

    if not _mean_reversion_regime(analysis):
        logger.info(f"{symbol}: Skipping - no strong trend, not choppy enough for mean-reversion")
        return None
    if not _mean_reversion_buy_signal(analysis):
        logger.info(f"{symbol}: Choppy regime, no mean-reversion signal (RSI={analysis.get('rsi_14')})")
        return f"{symbol}: choppy regime, no mean-reversion signal"

    if not _entry_caps_allow_buy(symbol, " [mean-reversion]"):
        return None
    if _would_sell_be_day_trade(symbol):
        logger.warning(f"{symbol}: Skipping mean-reversion BUY - already purchased today")
        return None

    equity = _get_account_equity()
    if equity is None:
        logger.warning(f"{symbol}: Skipping mean-reversion BUY - could not fetch account equity")
        return None
    # existing_position_value is always 0 here: this branch only runs when
    # held_qty == 0 (checked above), so there's nothing to look up.
    qty = _compute_mean_reversion_qty(analysis, equity, 0.0) if equity > 0 else MIN_SHARES
    if qty <= 0:
        logger.warning(f"{symbol}: Skipping mean-reversion BUY - position sizing returned 0")
        return None
    price = analysis.get("current_price") or 0
    order_value = qty * price
    buying_power = _get_buying_power()
    if order_value > 0 and buying_power < order_value:
        logger.warning(
            f"{symbol}: Skipping mean-reversion BUY - insufficient buying power "
            f"(${buying_power:.2f} < ${order_value:.2f})"
        )
        return None
    order = _build_qty_order(symbol, OrderSide.BUY, qty, price)
    if order is None:
        logger.warning(f"{symbol}: Skipping mean-reversion BUY - no usable reference price for limit order")
        return None
    verdict = _confirm_or_alert_veto(symbol, "BUY", analysis)
    if verdict is None:
        return None
    submitted, recorded_fill = _submit_order(
        symbol=symbol,
        order=order,
        side="BUY",
        source=MEAN_REVERSION_SOURCE,
        fallback_price=price,
        failure_prefix="Mean-reversion BUY order FAILED",
    )
    if not submitted:
        return None
    logger.info(f"Mean-reversion BUY submitted for {symbol} qty={qty} limit=${order.limit_price:.2f}")
    if recorded_fill:
        send_alert(f"Mean-reversion BUY {symbol} qty={qty}{_ai_alert_suffix(verdict)}", "trade")
    return None


def execute_trade(symbol: str, analysis: dict | None):
    if not analysis:
        logger.info(f"{symbol}: Skipping - no analysis")
        return

    # Risk exits always run first (even without strong_trend) so stops protect capital.
    if _try_risk_exit(symbol, analysis):
        return

    if not analysis.get("strong_trend", False):
        # Not trending: try the regime-switching mean-reversion counter-strategy
        # instead of sitting out every choppy stretch (no-op when disabled).
        return _try_mean_reversion(symbol, analysis)

    bullish_trigger = (
        analysis.get("bullish_crossover")
        or analysis.get("sar_flipped_to_bull")
        or analysis.get("bullish_crossover_recent", False)
        or analysis.get("sar_flipped_to_bull_recent", False)
    )

    # Core BUY decision — many gates are mode-dependent.
    core_ok = (
        analysis.get("trending_up_a_lot")
        and analysis.get("sar_below_price")
        and not analysis.get("similar_to_yesterday", False)
        and not analysis.get("bb_squeeze", False)
        and not analysis.get("avoid_long", False)
    )
    if REQUIRE_BULLISH_TRIGGER:
        core_ok = core_ok and bool(bullish_trigger)
    if REQUIRE_NEAR_UPPER_BAND:
        core_ok = core_ok and bool(analysis.get("near_upper_band"))
    if REQUIRE_ADX_RISING:
        core_ok = core_ok and bool(analysis.get("adx_rising"))
    if REQUIRE_VOLUME_CONFIRMATION:
        core_ok = core_ok and bool(analysis.get("volume_confirmed"))
    if LONG_TERM_SMA_PERIOD and LONG_TERM_SMA_PERIOD > 0:
        core_ok = core_ok and bool(analysis.get("above_long_term_ma"))

    if core_ok:
        # Daily/weekly caps apply to new entries only — never block exits.
        if not _entry_caps_allow_buy(symbol, ""):
            return
        if _would_sell_be_day_trade(symbol):
            logger.warning(
                f"{symbol}: Skipping BUY - already purchased today (one entry per symbol per day)"
            )
            return

        if NOTIONAL_PER_TRADE is not None and NOTIONAL_PER_TRADE >= 1:
            # Fractional mode: buy a fixed dollar amount, or 1 whole share if price <= notional
            buying_power = _get_buying_power()
            price = analysis.get("current_price") or 0

            # Same total-exposure cap as the risk-based qty path (_risk_based_qty):
            # don't add to a symbol already at or over MAX_POSITION_PCT_EQUITY.
            # Best-effort - an equity-fetch failure here just skips this extra
            # check rather than blocking an otherwise-small notional buy.
            if MAX_POSITION_PCT_EQUITY and price > 0:
                equity_for_cap = _get_account_equity()
                if equity_for_cap and equity_for_cap > 0:
                    existing_value = _existing_position_value(symbol, price)
                    if existing_value >= MAX_POSITION_PCT_EQUITY * equity_for_cap:
                        logger.warning(
                            f"{symbol}: Skipping BUY - already at or over MAX_POSITION_PCT_EQUITY "
                            f"(existing position worth ${existing_value:,.2f})"
                        )
                        return

            if price > 0 and price <= NOTIONAL_PER_TRADE and buying_power >= price:
                # Rules engine has already formed its own BUY assumption and this
                # specific order is affordable - only now ask the AI gate to
                # confirm it before any order is placed.
                order = _build_qty_order(symbol, OrderSide.BUY, 1, price)
                if order is None:
                    logger.warning(f"{symbol}: Skipping BUY - no usable reference price for limit order")
                    return
                verdict = _confirm_or_alert_veto(symbol, "BUY", analysis)
                if verdict is None:
                    return
                submitted, recorded_fill = _submit_order(
                    symbol=symbol,
                    order=order,
                    side="BUY",
                    source="ta",
                    fallback_price=price,
                    failure_prefix="BUY order FAILED",
                )
                if not submitted:
                    return
                logger.info(
                    f"BUY submitted for {symbol} qty=1 limit=${order.limit_price:.2f} "
                    f"(whole share, price ${price:.2f} <= ${NOTIONAL_PER_TRADE})"
                )
                if recorded_fill:
                    send_alert(f"BUY {symbol} 1 share @ ~${price:.2f}{_ai_alert_suffix(verdict)}", "trade")
            else:
                notional = min(float(NOTIONAL_PER_TRADE), buying_power) if buying_power > 0 else 0.0
                if notional < 1:
                    logger.warning(
                        f"{symbol}: Skipping BUY - notional ${notional:.2f} below Alpaca minimum $1"
                    )
                    return
                notional = round(notional, 2)
                verdict = _confirm_or_alert_veto(symbol, "BUY", analysis)
                if verdict is None:
                    return
                order = MarketOrderRequest(
                    symbol=symbol,
                    notional=notional,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
                submitted, recorded_fill = _submit_order(
                    symbol=symbol,
                    order=order,
                    side="BUY",
                    source="ta",
                    fallback_price=analysis.get("current_price"),
                    failure_prefix="BUY order FAILED",
                )
                if not submitted:
                    return
                logger.info(f"BUY submitted for {symbol} notional=${notional:.2f}")
                if recorded_fill:
                    send_alert(f"BUY {symbol} ${notional:.2f}{_ai_alert_suffix(verdict)}", "trade")
        else:
            equity = _get_account_equity()
            if equity is None:
                logger.warning(f"{symbol}: Skipping BUY - could not fetch account equity")
                return
            price = analysis.get("current_price") or 0
            existing_value = _existing_position_value(symbol, price) if price > 0 else 0.0
            qty = _compute_buy_qty(analysis, equity, existing_value) if equity > 0 else MIN_SHARES
            if qty <= 0:
                logger.warning(
                    f"{symbol}: Skipping BUY - already at or over MAX_POSITION_PCT_EQUITY "
                    f"(existing position worth ${existing_value:,.2f})"
                )
                return
            order_value = qty * price
            buying_power = _get_buying_power()
            if order_value > 0 and buying_power < order_value:
                logger.warning(
                    f"{symbol}: Skipping BUY - insufficient buying power "
                    f"(${buying_power:.2f} < ${order_value:.2f})"
                )
                return
            order = _build_qty_order(symbol, OrderSide.BUY, qty, price)
            if order is None:
                logger.warning(f"{symbol}: Skipping BUY - no usable reference price for limit order")
                return
            verdict = _confirm_or_alert_veto(symbol, "BUY", analysis)
            if verdict is None:
                return
            submitted, recorded_fill = _submit_order(
                symbol=symbol,
                order=order,
                side="BUY",
                source="ta",
                fallback_price=analysis.get("current_price"),
                failure_prefix="BUY order FAILED",
            )
            if not submitted:
                return
            logger.info(f"BUY submitted for {symbol} qty={qty} limit=${order.limit_price:.2f}")
            if recorded_fill:
                send_alert(f"BUY {symbol} qty={qty}{_ai_alert_suffix(verdict)}", "trade")

    elif _ta_sell_signal(analysis):
        try:
            position = trading_client.get_open_position(symbol)
            qty = float(position.qty)
        except Exception as e:
            if "position does not exist" not in str(e).lower() and "not found" not in str(e).lower():
                logger.error(f"Position check failed for {symbol}: {e}")
            qty = 0
        if qty > 0:
            if _should_block_sell_min_hold(symbol):
                logger.info(
                    f"{symbol}: Skipping signal SELL - min hold not met "
                    f"(need {MIN_HOLD_HOURS}h); stop-loss still active"
                )
            elif _should_block_sell_pdt(symbol):
                logger.warning(
                    f"{symbol}: Skipping signal SELL - PDT limit reached "
                    f"({_count_day_trades_in_last_5_days()}/{MAX_DAY_TRADES_IN_5_DAYS} day trades in 5 days)"
                )
                send_alert(
                    f"{symbol}: Signal SELL skipped (PDT limit). Consider closing tomorrow.",
                    "error",
                )
            else:
                sell_price = analysis.get("current_price")
                order = _build_qty_order(symbol, OrderSide.SELL, qty, sell_price)
                if order is None:
                    logger.warning(f"{symbol}: Skipping signal SELL - no usable reference price for limit order")
                    return

                # Rules engine has already formed its own SELL assumption above -
                # only now ask the AI gate to confirm that specific call before
                # any order is placed. (Stop-loss/trailing-stop exits above this
                # function never go through this gate - they must never be delayed
                # or blocked by an external call.)
                verdict = _confirm_or_alert_veto(symbol, "SELL", analysis)
                if verdict is None:
                    return

                submitted, recorded_fill = _submit_order(
                    symbol=symbol,
                    order=order,
                    side="SELL",
                    source="ta",
                    fallback_price=sell_price,
                    failure_prefix="SELL order FAILED",
                )
                if not submitted:
                    return
                logger.info(f"SELL submitted for {symbol} limit=${order.limit_price:.2f}")
                if recorded_fill:
                    send_alert(f"SELL {symbol} qty={qty:.4g}{_ai_alert_suffix(verdict)}", "trade")
    else:
        reasons = _skip_reasons_buy(analysis)
        scorecard = _buy_gate_scorecard(analysis)
        logger.info(f"{symbol}: No signal - {scorecard} | {', '.join(reasons)}")
        return f"{symbol}: {scorecard}"


# ─── Signal-based execution (external oracle, bypasses TA gates) ───────────────


def execute_signal_buy(symbol: str) -> None:
    """
    Buy symbol based on an external signal. Skips all TA gates but still
    respects daily/weekly trade caps, open-position limit, and notional sizing.
    """
    if not _entry_caps_allow_buy(symbol, " [signal]"):
        return

    # Don't double-buy a symbol we're already holding.
    try:
        position = trading_client.get_open_position(symbol)
        if float(position.qty) > 0:
            logger.info(f"{symbol} [signal]: Already holding, skipping BUY")
            return
    except Exception as e:
        if "position does not exist" not in str(e).lower() and "not found" not in str(e).lower():
            logger.error(f"{symbol} [signal]: Position check failed: {e}")
            return

    buying_power = _get_buying_power()

    if NOTIONAL_PER_TRADE is not None and NOTIONAL_PER_TRADE >= 1:
        price = get_intraday_price(symbol)
        if price and price > 0 and price <= NOTIONAL_PER_TRADE and buying_power >= price:
            # Whole share when it fits inside our notional target.
            order = _build_qty_order(symbol, OrderSide.BUY, 1, price)
            if order is None:
                logger.warning(f"{symbol} [signal]: Skipping BUY - no usable reference price for limit order")
                return
            submitted, recorded_fill = _submit_order(
                symbol=symbol,
                order=order,
                side="BUY",
                source="signal",
                fallback_price=price,
                failure_prefix="[signal] BUY order FAILED",
            )
            if not submitted:
                return
            logger.info(f"{symbol} [signal]: BUY submitted qty=1 limit=${order.limit_price:.2f} (whole share @ ~${price:.2f})")
            if recorded_fill:
                send_alert(f"[signal] BUY {symbol} 1 share @ ~${price:.2f}", "trade")
        else:
            notional = min(float(NOTIONAL_PER_TRADE), buying_power) if buying_power > 0 else 0.0
            if notional < 1:
                logger.warning(f"{symbol} [signal]: Skipping BUY - notional ${notional:.2f} below Alpaca minimum $1")
                return
            notional = round(notional, 2)
            order = MarketOrderRequest(
                symbol=symbol, notional=notional, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
            )
            submitted, recorded_fill = _submit_order(
                symbol=symbol,
                order=order,
                side="BUY",
                source="signal",
                fallback_price=price,
                failure_prefix="[signal] BUY order FAILED",
            )
            if not submitted:
                return
            logger.info(f"{symbol} [signal]: BUY submitted notional=${notional:.2f}")
            if recorded_fill:
                send_alert(f"[signal] BUY {symbol} ${notional:.2f}", "trade")
    else:
        # Qty mode (no notional configured): buy 1 share.
        intraday_price = get_intraday_price(symbol)
        order = _build_qty_order(symbol, OrderSide.BUY, 1, intraday_price)
        if order is None:
            logger.warning(f"{symbol} [signal]: Skipping BUY - no usable reference price for limit order")
            return
        submitted, recorded_fill = _submit_order(
            symbol=symbol,
            order=order,
            side="BUY",
            source="signal",
            fallback_price=intraday_price,
            failure_prefix="[signal] BUY order FAILED",
        )
        if not submitted:
            return
        logger.info(f"{symbol} [signal]: BUY submitted qty=1 limit=${order.limit_price:.2f}")
        if recorded_fill:
            send_alert(f"[signal] BUY {symbol} qty=1", "trade")


def execute_signal_sell(symbol: str) -> None:
    """
    Sell symbol based on an external signal.
    Requires a minimum hold (mode MIN_HOLD_HOURS, defaulting to 24h if unset)
    so we never flip a same-day buy into a day trade from a signal change.
    """
    last_buy_ts = _last_buy_ts(symbol)
    if last_buy_ts is None:
        logger.info(f"{symbol} [signal]: No buy record found, skipping signal SELL")
        return

    held_seconds = time.time() - last_buy_ts
    # Signal path always enforces at least 24h; modes may require longer via MIN_HOLD_HOURS.
    min_hours = max(24.0, float(MIN_HOLD_HOURS or 0))
    if held_seconds < min_hours * 3600:
        logger.info(
            f"{symbol} [signal]: Skipping SELL - held only {held_seconds / 3600:.1f}h "
            f"(need {min_hours:.0f}h)"
        )
        return

    if _should_block_sell_pdt(symbol):
        logger.warning(f"{symbol} [signal]: Skipping SELL - PDT limit reached")
        send_alert(f"{symbol}: [signal] SELL skipped (PDT limit).", "error")
        return

    try:
        position = trading_client.get_open_position(symbol)
        qty = float(position.qty)
    except Exception as e:
        if "position does not exist" not in str(e).lower() and "not found" not in str(e).lower():
            logger.error(f"{symbol} [signal]: Position check failed: {e}")
        return

    if qty <= 0:
        return

    sell_price = get_intraday_price(symbol)
    order = _build_qty_order(symbol, OrderSide.SELL, qty, sell_price)
    if order is None:
        logger.warning(f"{symbol} [signal]: Skipping SELL - no usable reference price for limit order")
        return
    submitted, recorded_fill = _submit_order(
        symbol=symbol,
        order=order,
        side="SELL",
        source="signal",
        fallback_price=sell_price,
        failure_prefix="[signal] SELL order FAILED",
    )
    if not submitted:
        return
    logger.info(f"{symbol} [signal]: SELL submitted qty={qty:.4g} limit=${order.limit_price:.2f}")
    if recorded_fill:
        send_alert(f"[signal] SELL {symbol} qty={qty:.4g}", "trade")