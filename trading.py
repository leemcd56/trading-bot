# trading.py
import os
import time
from collections import defaultdict
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
)

load_dotenv()

_base_url = os.getenv("ALPACA_BASE_URL")
trading_client = TradingClient(
    api_key=os.getenv('ALPACA_API_KEY'),
    secret_key=os.getenv('ALPACA_SECRET_KEY'),
    paper=True,   # Change to False only when going live (very carefully!)
    url_override=_base_url if _base_url else None,
)

TRADE_LOG_TABLE = "trade_log"
TRAIL_STATE_TABLE = "trail_state"


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


def _count_daily() -> int:
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        now = time.time()
        day_ago = now - 86400
        out = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
            [day_ago],
        ).fetchone()
        return out[0] if out else 0
    except Exception as e:
        logger.warning(f"Could not read trade log for daily count: {e}")
        return 0
    finally:
        con.close()


def _count_weekly() -> int:
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        now = time.time()
        week_ago = now - 7 * 86400
        out = con.execute(
            f"SELECT COUNT(*) FROM {TRADE_LOG_TABLE} WHERE timestamp_utc >= ?",
            [week_ago],
        ).fetchone()
        return out[0] if out else 0
    except Exception as e:
        logger.warning(f"Could not read trade log for weekly count: {e}")
        return 0
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


def _open_positions_count() -> int:
    try:
        positions = trading_client.get_all_positions()
        watched = {s.upper() for s in SYMBOLS}
        return sum(1 for p in positions if p.symbol.upper() in watched and float(p.qty) > 0)
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return 0


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
    if not bullish_trigger:
        reasons.append("no_bullish_crossover_or_sar_flip")
    if analysis.get('similar_to_yesterday'):
        reasons.append("similar_to_yesterday=True")
    if analysis.get('bb_squeeze'):
        reasons.append("bb_squeeze=True")
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
        ("trigger", bool(bullish_trigger)),
        ("!similar", not bool(analysis.get('similar_to_yesterday', False))),
        ("!squeeze", not bool(analysis.get('bb_squeeze', False))),
        ("!avoid", not bool(analysis.get('avoid_long', False))),
    ]
    return " ".join([f"{name}={'Y' if ok else 'N'}" for name, ok in gates])


def execute_trade(symbol: str, analysis: dict | None):
    if not analysis or not analysis.get('strong_trend', False):
        logger.info(f"{symbol}: Skipping - no strong trend")
        return

    # ─── Risk limits ───
    if _count_daily() >= MAX_DAILY_TRADES:
        logger.warning(f"{symbol}: Skipping - daily trade cap reached ({_count_daily()}/{MAX_DAILY_TRADES})")
        return
    if _count_weekly() >= MAX_WEEKLY_TRADES:
        logger.warning(f"{symbol}: Skipping - weekly trade cap reached ({_count_weekly()}/{MAX_WEEKLY_TRADES})")
        return

    # ─── Stop-loss: sell if position is down STOP_LOSS_PCT from entry ───
    try:
        position = trading_client.get_open_position(symbol)
        qty = float(position.qty)
        if qty > 0:
            entry = float(position.avg_entry_price)
            current = analysis.get("current_price") or 0
            if entry > 0 and current > 0 and current <= entry * (1 - STOP_LOSS_PCT):
                if _should_block_sell_pdt(symbol):
                    logger.warning(
                        f"{symbol}: Skipping stop-loss SELL - PDT limit reached ({_count_day_trades_in_last_5_days()}/{MAX_DAY_TRADES_IN_5_DAYS} day trades in 5 days)"
                    )
                    send_alert(
                        f"{symbol}: Stop-loss skipped (PDT limit). Consider closing tomorrow.",
                        "error",
                    )
                    return
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                try:
                    trading_client.submit_order(order)
                except Exception as order_err:
                    logger.error(f"Stop-loss order submission failed for {symbol}: {order_err}")
                    send_alert(f"Stop-loss order FAILED for {symbol}: {order_err}", "error")
                    return
                _record_trade(symbol, "SELL", qty)
                _clear_trail_state(symbol)
                logger.warning(f"Stop-loss SELL for {symbol}: price {current:.2f} <= entry {entry:.2f} * (1 - {STOP_LOSS_PCT:.0%})")
                send_alert(f"Stop-loss SELL {symbol} qty={qty:.4g} @ {current:.2f} (entry {entry:.2f})", "trade")
                return
            # ─── Trailing stop: after fixed stop-loss, lock in gains once price is TRAIL_ACTIVATION_PCT above entry ───
            running_high = _get_trail_running_high(symbol)
            if running_high is None:
                running_high = current
            else:
                running_high = max(running_high, current)
            _set_trail_running_high(symbol, running_high)
            trail_active = current >= entry * (1 + TRAIL_ACTIVATION_PCT)
            if trail_active and current <= running_high * (1 - TRAIL_PCT):
                if _should_block_sell_pdt(symbol):
                    logger.warning(
                        f"{symbol}: Skipping trailing-stop SELL - PDT limit reached ({_count_day_trades_in_last_5_days()}/{MAX_DAY_TRADES_IN_5_DAYS} day trades in 5 days)"
                    )
                    send_alert(f"{symbol}: Trailing-stop skipped (PDT limit). Consider closing tomorrow.", "error")
                    return
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                try:
                    trading_client.submit_order(order)
                except Exception as order_err:
                    logger.error(f"Trailing-stop order submission failed for {symbol}: {order_err}")
                    send_alert(f"Trailing-stop order FAILED for {symbol}: {order_err}", "error")
                    return
                _record_trade(symbol, "SELL", qty)
                _clear_trail_state(symbol)
                logger.warning(f"Trailing-stop SELL for {symbol}: price {current:.2f} <= running_high {running_high:.2f} * (1 - {TRAIL_PCT:.0%})")
                send_alert(f"Trailing-stop SELL {symbol} qty={qty:.4g} @ {current:.2f} (running_high {running_high:.2f})", "trade")
                return
    except Exception as e:
        if "position does not exist" not in str(e).lower():
            logger.error(f"Position check failed for {symbol}: {e}")

    bullish_trigger = (
        analysis.get('bullish_crossover')
        or analysis.get('sar_flipped_to_bull')
        or analysis.get('bullish_crossover_recent', False)
        or analysis.get('sar_flipped_to_bull_recent', False)
    )

    if (
        analysis.get('trending_up_a_lot') and
        analysis.get('sar_below_price') and
        bullish_trigger and
        not analysis.get('similar_to_yesterday', False) and
        not analysis.get('bb_squeeze', False) and
        not analysis.get('avoid_long', False)
    ):
        if _open_positions_count() >= MAX_OPEN_POSITIONS:
            logger.warning(f"{symbol}: Skipping BUY - max open positions ({MAX_OPEN_POSITIONS})")
            return
        if NOTIONAL_PER_TRADE is not None and NOTIONAL_PER_TRADE >= 1:
            # Fractional mode: buy a fixed dollar amount, or 1 whole share if price <= notional
            buying_power = _get_buying_power()
            price = analysis.get("current_price") or 0
            if price > 0 and price <= NOTIONAL_PER_TRADE and buying_power >= price:
                # Buy one whole share when it costs less than or equal to our notional target
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=1,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY
                )
                try:
                    trading_client.submit_order(order)
                except Exception as order_err:
                    logger.error(f"BUY order submission failed for {symbol}: {order_err}")
                    send_alert(f"BUY order FAILED for {symbol}: {order_err}", "error")
                    return
                _record_trade(symbol, "BUY", 1)
                logger.info(f"BUY submitted for {symbol} qty=1 (whole share, price ${price:.2f} <= ${NOTIONAL_PER_TRADE})")
                send_alert(f"BUY {symbol} 1 share @ ~${price:.2f}", "trade")
            else:
                notional = min(float(NOTIONAL_PER_TRADE), buying_power) if buying_power > 0 else 0.0
                if notional < 1:
                    logger.warning(f"{symbol}: Skipping BUY - notional ${notional:.2f} below Alpaca minimum $1")
                    return
                notional = round(notional, 2)
                order = MarketOrderRequest(
                    symbol=symbol,
                    notional=notional,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY
                )
                try:
                    trading_client.submit_order(order)
                except Exception as order_err:
                    logger.error(f"BUY order submission failed for {symbol}: {order_err}")
                    send_alert(f"BUY order FAILED for {symbol}: {order_err}", "error")
                    return
                _record_trade(symbol, "BUY", 0)
                logger.info(f"BUY submitted for {symbol} notional=${notional:.2f}")
                send_alert(f"BUY {symbol} ${notional:.2f}", "trade")
        else:
            equity = _get_account_equity()
            if equity is None:
                logger.warning(f"{symbol}: Skipping BUY - could not fetch account equity")
                return
            qty = _compute_buy_qty(analysis, equity) if equity > 0 else MIN_SHARES
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            )
            try:
                trading_client.submit_order(order)
            except Exception as order_err:
                logger.error(f"BUY order submission failed for {symbol}: {order_err}")
                send_alert(f"BUY order FAILED for {symbol}: {order_err}", "error")
                return
            _record_trade(symbol, "BUY", qty)
            logger.info(f"BUY submitted for {symbol} qty={qty}")
            send_alert(f"BUY {symbol} qty={qty}", "trade")

    elif (
        analysis.get('near_lower_band') or
        analysis.get('sar_above_price') or
        analysis.get('sar_flipped_to_bear') or
        analysis.get('dive_bombing') or
        analysis.get('bearish_crossover')
    ):
        try:
            position = trading_client.get_open_position(symbol)
            qty = float(position.qty)
        except Exception as e:
            if "position does not exist" not in str(e).lower():
                logger.error(f"Position check failed for {symbol}: {e}")
            qty = 0
        if qty > 0:
            if _should_block_sell_pdt(symbol):
                logger.warning(
                    f"{symbol}: Skipping signal SELL - PDT limit reached ({_count_day_trades_in_last_5_days()}/{MAX_DAY_TRADES_IN_5_DAYS} day trades in 5 days)"
                )
                send_alert(f"{symbol}: Signal SELL skipped (PDT limit). Consider closing tomorrow.", "error")
            else:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                try:
                    trading_client.submit_order(order)
                except Exception as order_err:
                    logger.error(f"SELL order submission failed for {symbol}: {order_err}")
                    send_alert(f"SELL order FAILED for {symbol}: {order_err}", "error")
                    return
                _record_trade(symbol, "SELL", qty)
                _clear_trail_state(symbol)
                logger.info(f"SELL submitted for {symbol}")
                send_alert(f"SELL {symbol} qty={qty:.4g}", "trade")
    else:
        reasons = _skip_reasons_buy(analysis)
        scorecard = _buy_gate_scorecard(analysis)
        logger.info(f"{symbol}: No signal - {scorecard} | {', '.join(reasons)}")
        send_alert(f"{symbol}: No signal — {scorecard} | {', '.join(reasons)}", "hodl")


# ─── Signal-based execution (external oracle, bypasses TA gates) ───────────────


def execute_signal_buy(symbol: str) -> None:
    """
    Buy symbol based on an external signal. Skips all TA gates but still
    respects daily/weekly trade caps, open-position limit, and notional sizing.
    """
    if _count_daily() >= MAX_DAILY_TRADES:
        logger.warning(f"{symbol} [signal]: Skipping BUY - daily cap ({_count_daily()}/{MAX_DAILY_TRADES})")
        return
    if _count_weekly() >= MAX_WEEKLY_TRADES:
        logger.warning(f"{symbol} [signal]: Skipping BUY - weekly cap ({_count_weekly()}/{MAX_WEEKLY_TRADES})")
        return
    if _open_positions_count() >= MAX_OPEN_POSITIONS:
        logger.warning(f"{symbol} [signal]: Skipping BUY - max open positions ({MAX_OPEN_POSITIONS})")
        return

    # Don't double-buy a symbol we're already holding.
    try:
        position = trading_client.get_open_position(symbol)
        if float(position.qty) > 0:
            logger.info(f"{symbol} [signal]: Already holding, skipping BUY")
            return
    except Exception as e:
        if "position does not exist" not in str(e).lower():
            logger.error(f"{symbol} [signal]: Position check failed: {e}")

    buying_power = _get_buying_power()

    if NOTIONAL_PER_TRADE is not None and NOTIONAL_PER_TRADE >= 1:
        price = get_intraday_price(symbol)
        if price and price > 0 and price <= NOTIONAL_PER_TRADE and buying_power >= price:
            # Whole share when it fits inside our notional target.
            order = MarketOrderRequest(
                symbol=symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
            )
            try:
                trading_client.submit_order(order)
            except Exception as order_err:
                logger.error(f"{symbol} [signal]: BUY order failed: {order_err}")
                send_alert(f"[signal] BUY order FAILED for {symbol}: {order_err}", "error")
                return
            _record_trade(symbol, "BUY", 1)
            logger.info(f"{symbol} [signal]: BUY submitted qty=1 (whole share @ ~${price:.2f})")
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
            try:
                trading_client.submit_order(order)
            except Exception as order_err:
                logger.error(f"{symbol} [signal]: BUY order failed: {order_err}")
                send_alert(f"[signal] BUY order FAILED for {symbol}: {order_err}", "error")
                return
            _record_trade(symbol, "BUY", 0)
            logger.info(f"{symbol} [signal]: BUY submitted notional=${notional:.2f}")
            send_alert(f"[signal] BUY {symbol} ${notional:.2f}", "trade")
    else:
        # Qty mode (no notional configured): buy 1 share.
        order = MarketOrderRequest(
            symbol=symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY
        )
        try:
            trading_client.submit_order(order)
        except Exception as order_err:
            logger.error(f"{symbol} [signal]: BUY order failed: {order_err}")
            send_alert(f"[signal] BUY order FAILED for {symbol}: {order_err}", "error")
            return
        _record_trade(symbol, "BUY", 1)
        logger.info(f"{symbol} [signal]: BUY submitted qty=1")
        send_alert(f"[signal] BUY {symbol} qty=1", "trade")


def execute_signal_sell(symbol: str) -> None:
    """
    Sell symbol based on an external signal.
    Requires the position to have been held for at least 24 hours so we
    never flip a same-day buy into a day trade from a signal change.
    """
    # 24-hour minimum hold: look up the most recent BUY in the trade log.
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        out = con.execute(
            f"SELECT MAX(timestamp_utc) FROM {TRADE_LOG_TABLE} WHERE symbol = ? AND side = 'BUY'",
            [symbol],
        ).fetchone()
        last_buy_ts = float(out[0]) if out and out[0] is not None else None
    finally:
        con.close()

    if last_buy_ts is None:
        logger.info(f"{symbol} [signal]: No buy record found, skipping signal SELL")
        return

    held_seconds = time.time() - last_buy_ts
    if held_seconds < 86400:
        logger.info(
            f"{symbol} [signal]: Skipping SELL - held only {held_seconds / 3600:.1f}h (need 24h)"
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
        if "position does not exist" not in str(e).lower():
            logger.error(f"{symbol} [signal]: Position check failed: {e}")
        return

    if qty <= 0:
        return

    order = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY
    )
    try:
        trading_client.submit_order(order)
    except Exception as order_err:
        logger.error(f"{symbol} [signal]: SELL order failed: {order_err}")
        send_alert(f"[signal] SELL order FAILED for {symbol}: {order_err}", "error")
        return
    _record_trade(symbol, "SELL", qty)
    _clear_trail_state(symbol)
    logger.info(f"{symbol} [signal]: SELL submitted qty={qty:.4g}")
    send_alert(f"[signal] SELL {symbol} qty={qty:.4g}", "trade")