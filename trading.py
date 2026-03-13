# trading.py
import os
import time
import duckdb
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv
from utils import logger
from alerts import send_alert
from config import (
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
)

load_dotenv()

trading_client = TradingClient(
    api_key=os.getenv('ALPACA_API_KEY'),
    secret_key=os.getenv('ALPACA_SECRET_KEY'),
    paper=True   # Change to False only when going live (very carefully!)
)

TRADE_LOG_TABLE = "trade_log"


def _ensure_trade_log(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {TRADE_LOG_TABLE} (
            timestamp_utc DOUBLE,
            symbol VARCHAR,
            side VARCHAR
        )
    """)


def _record_trade(symbol: str, side: str) -> None:
    """Persist trade to DuckDB for daily/weekly limit counts."""
    con = duckdb.connect(DB_PATH)
    try:
        _ensure_trade_log(con)
        ts = time.time()
        con.execute(
            f"INSERT INTO {TRADE_LOG_TABLE} (timestamp_utc, symbol, side) VALUES (?, ?, ?)",
            [ts, symbol, side],
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


def _open_positions_count() -> int:
    try:
        positions = trading_client.get_all_positions()
        return sum(1 for p in positions if float(p.qty) > 0)
    except Exception as e:
        logger.error(f"Failed to get positions: {e}")
        return 0


def _get_account_equity() -> float:
    """Return current account equity (portfolio value). Returns 0 on error."""
    try:
        account = trading_client.get_account()
        # Alpaca returns equity as string
        return float(account.equity or 0)
    except Exception as e:
        logger.warning(f"Failed to get account equity: {e}")
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
    if not analysis.get('near_upper_band'):
        reasons.append("near_upper_band=False")
    if not analysis.get('sar_below_price'):
        reasons.append("sar_below_price=False")
    if not analysis.get('bullish_crossover') and not analysis.get('sar_flipped_to_bull'):
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
        position = trading_client.get_position(symbol)
        qty = float(position.qty)
        if qty > 0:
            entry = float(position.avg_entry_price)
            current = analysis.get("current_price") or 0
            if entry > 0 and current > 0 and current <= entry * (1 - STOP_LOSS_PCT):
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order)
                _record_trade(symbol, "SELL")
                logger.warning(f"Stop-loss SELL for {symbol}: price {current:.2f} <= entry {entry:.2f} * (1 - {STOP_LOSS_PCT:.0%})")
                send_alert(f"Stop-loss SELL {symbol} qty={qty:.0f} @ {current:.2f} (entry {entry:.2f})", "trade")
                return
    except Exception as e:
        if "position does not exist" not in str(e).lower():
            logger.debug(f"No position or error for {symbol}: {e}")

    if (
        analysis.get('trending_up_a_lot') and
        analysis.get('near_upper_band') and
        analysis.get('sar_below_price') and
        (analysis.get('bullish_crossover') or analysis.get('sar_flipped_to_bull')) and
        not analysis.get('similar_to_yesterday', False) and
        not analysis.get('bb_squeeze', False) and
        not analysis.get('avoid_long', False)
    ):
        if _open_positions_count() >= MAX_OPEN_POSITIONS:
            logger.warning(f"{symbol}: Skipping BUY - max open positions ({MAX_OPEN_POSITIONS})")
            return
        equity = _get_account_equity()
        qty = _compute_buy_qty(analysis, equity) if equity > 0 else MIN_SHARES
        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order)
        _record_trade(symbol, "BUY")
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
            position = trading_client.get_position(symbol)
            qty = float(position.qty)
            if qty > 0:
                order = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY
                )
                trading_client.submit_order(order)
                _record_trade(symbol, "SELL")
                logger.info(f"SELL submitted for {symbol}")
                send_alert(f"SELL {symbol} qty={qty:.0f}", "trade")
        except Exception as e:
            if "position does not exist" not in str(e).lower():
                logger.error(f"Position check failed for {symbol}: {e}")
    else:
        reasons = _skip_reasons_buy(analysis)
        logger.info(f"{symbol}: No signal - {', '.join(reasons)}")