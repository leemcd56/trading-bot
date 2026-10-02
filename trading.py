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
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv
from utils import logger
from alerts import send_alert
from data_providers import get_intraday_price
from config import (
    SYMBOLS,
    MAX_DAILY_TRADES,
    MAX_WEEKLY_TRADES,
    MAX_OPEN_POSITIONS,
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

TRADE_LOG_TABLE = "trade_log"
TRAIL_STATE_TABLE = "trail_state"
TRADE_HISTORY_TABLE = "trade_history"
PENDING_ORDERS_TABLE = "pending_orders"


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
    _record_trade(symbol, side, delta)
    _record_trade_history(symbol, side, delta, _order_fill_price(order, fallback_price), source)
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


def _open_positions_count() -> int | None:
    try:
        positions = trading_client.get_all_positions()
        return sum(1 for p in positions if float(p.qty) > 0)
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return None


def get_open_position_symbols() -> set[str] | None:
    """Return held symbols with positive quantity, or None on API failure."""
    try:
        positions = trading_client.get_all_positions()
        return {
            str(p.symbol).upper()
            for p in positions
            if _to_float(getattr(p, "qty", 0)) and float(p.qty) > 0
        }
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return None


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


def _compute_buy_qty(analysis: dict, equity: float) -> int:
    """
    Compute number of shares to buy using risk-based position sizing.
    Risk per trade = RISK_PCT_PER_TRADE * equity.
    Stop distance per share = max(ATR_14, current_price * STOP_LOSS_PCT).
    qty = risk_amount / stop_distance_per_share, rounded down, clamped to MIN/MAX_SHARES and max position value.
    If RISK_PCT_PER_TRADE is None or equity/analysis invalid, returns 1.
    """
    if RISK_PCT_PER_TRADE is None or RISK_PCT_PER_TRADE <= 0:
        return 1
    price = analysis.get("current_price") or 0
    if price <= 0 or equity <= 0:
        return MIN_SHARES
    atr = analysis.get("atr_14")
    stop_distance = price * STOP_LOSS_PCT
    if atr is not None and atr > 0:
        stop_distance = max(stop_distance, atr)
    if stop_distance <= 0:
        return MIN_SHARES
    risk_amount = RISK_PCT_PER_TRADE * equity
    # position_value * (stop_distance / price) = risk_amount  =>  position_value = risk_amount * price / stop_distance
    position_value = risk_amount * price / stop_distance
    qty = int(position_value / price)
    max_value = MAX_POSITION_PCT_EQUITY * equity if MAX_POSITION_PCT_EQUITY else position_value
    max_qty_by_value = int(max_value / price) if price > 0 else 0
    qty = min(qty, max_qty_by_value, MAX_SHARES)
    qty = max(qty, MIN_SHARES)
    return qty


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

    open_positions = _open_positions_count()
    if open_positions is None:
        logger.error(f"{symbol}{source}: Refusing BUY - open positions count unavailable")
        return False
    if open_positions >= MAX_OPEN_POSITIONS:
        logger.warning(
            f"{symbol}{source}: Skipping BUY - max open positions ({MAX_OPEN_POSITIONS})"
        )
        return False

    return True


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

        # ─── Stop-loss ───
        if current <= entry * (1 - STOP_LOSS_PCT):
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
                f"* (1 - {STOP_LOSS_PCT:.0%})"
            )
            if recorded_fill:
                send_alert(
                    f"Stop-loss SELL {symbol} qty={qty:.4g} @ {current:.2f} (entry {entry:.2f})",
                    "trade",
                )
            else:
                logger.info(f"{symbol}: Stop-loss order submitted; waiting for fill before logging trade")
            return True

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


def execute_trade(symbol: str, analysis: dict | None):
    if not analysis:
        logger.info(f"{symbol}: Skipping - no analysis")
        return

    # Risk exits always run first (even without strong_trend) so stops protect capital.
    if _try_risk_exit(symbol, analysis):
        return

    if not analysis.get("strong_trend", False):
        logger.info(f"{symbol}: Skipping - no strong trend")
        return

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
            if price > 0 and price <= NOTIONAL_PER_TRADE and buying_power >= price:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=1,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
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
                    f"BUY submitted for {symbol} qty=1 "
                    f"(whole share, price ${price:.2f} <= ${NOTIONAL_PER_TRADE})"
                )
                if recorded_fill:
                    send_alert(f"BUY {symbol} 1 share @ ~${price:.2f}", "trade")
            else:
                notional = min(float(NOTIONAL_PER_TRADE), buying_power) if buying_power > 0 else 0.0
                if notional < 1:
                    logger.warning(
                        f"{symbol}: Skipping BUY - notional ${notional:.2f} below Alpaca minimum $1"
                    )
                    return
                notional = round(notional, 2)
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
                    send_alert(f"BUY {symbol} ${notional:.2f}", "trade")
        else:
            equity = _get_account_equity()
            if equity is None:
                logger.warning(f"{symbol}: Skipping BUY - could not fetch account equity")
                return
            qty = _compute_buy_qty(analysis, equity) if equity > 0 else MIN_SHARES
            price = analysis.get("current_price") or 0
            order_value = qty * price
            buying_power = _get_buying_power()
            if order_value > 0 and buying_power < order_value:
                logger.warning(
                    f"{symbol}: Skipping BUY - insufficient buying power "
                    f"(${buying_power:.2f} < ${order_value:.2f})"
                )
                return
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
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
            logger.info(f"BUY submitted for {symbol} qty={qty}")
            if recorded_fill:
                send_alert(f"BUY {symbol} qty={qty}", "trade")

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
                    source="ta",
                    fallback_price=analysis.get("current_price"),
                    failure_prefix="SELL order FAILED",
                )
                if not submitted:
                    return
                logger.info(f"SELL submitted for {symbol}")
                if recorded_fill:
                    send_alert(f"SELL {symbol} qty={qty:.4g}", "trade")
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
            order = MarketOrderRequest(
                symbol=symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
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
            logger.info(f"{symbol} [signal]: BUY submitted qty=1 (whole share @ ~${price:.2f})")
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
        order = MarketOrderRequest(
            symbol=symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
        )
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
        logger.info(f"{symbol} [signal]: BUY submitted qty=1")
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

    order = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
    )
    sell_price = get_intraday_price(symbol)
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
    logger.info(f"{symbol} [signal]: SELL submitted qty={qty:.4g}")
    if recorded_fill:
        send_alert(f"[signal] SELL {symbol} qty={qty:.4g}", "trade")