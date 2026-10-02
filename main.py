import time
import datetime
import schedule
import pytz
from data_fetch import fetch_and_store, prune_old_trends
from analysis import analyze_trends
from trading import (
    execute_trade,
    execute_signal_buy,
    execute_signal_sell,
    get_locally_known_held_symbols,
    get_open_position_symbols,
    prune_old_trade_log,
    reconcile_pending_orders,
)
from migrations import init_db
from signals import fetch_signals
from utils import logger, is_market_open
from alerts import send_alert
from report import snapshot_portfolio, send_eod_summary
from config import (
    SYMBOLS,
    CHECK_INTERVAL_MINUTES,
    FMP_CHECK_INTERVAL_MINUTES,
    TRADING_MODE,
    MAX_DAILY_TRADES,
    MAX_WEEKLY_TRADES,
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_RISK_PCT,
    MAX_POSITION_CORRELATION,
    MAX_SLIPPAGE_PCT,
    STOP_LOSS_PCT,
    TRAIL_ACTIVATION_PCT,
    TRAIL_PCT,
    NOTIONAL_PER_TRADE,
    ADX_STRONG_TREND_THRESHOLD,
    MEAN_REVERSION_ENABLED,
)

_ET = pytz.timezone("US/Eastern")
_LAST_KNOWN_HELD_SYMBOLS: set[str] = set()


def _ta_symbols() -> list[str]:
    """Analyze both the watch list and any currently held symbols for stop coverage."""
    global _LAST_KNOWN_HELD_SYMBOLS

    symbols = {s.upper() for s in SYMBOLS}
    held_symbols = get_open_position_symbols()
    if held_symbols is not None:
        _LAST_KNOWN_HELD_SYMBOLS = set(held_symbols)
        symbols.update(held_symbols)
        return sorted(symbols)

    fallback_symbols = set(_LAST_KNOWN_HELD_SYMBOLS)
    local_held_symbols = get_locally_known_held_symbols()
    if local_held_symbols:
        fallback_symbols.update(local_held_symbols)
    if fallback_symbols:
        logger.warning(
            "Open-positions read failed; reusing known held symbols for risk checks: %s",
            sorted(fallback_symbols),
        )
        symbols.update(fallback_symbols)
    return sorted(symbols)


def fmp_job():
    """
    Independent FMP signal job — runs on its own schedule, separate from the TA loop.
    When analyst upgrades/downgrades warrant a trade, this fires it immediately without
    waiting for the next TA tick, but still respects all safety limits (daily cap,
    weekly cap, open-position cap, notional sizing, PDT guard).
    """
    if not is_market_open():
        return
    try:
        reconcile_pending_orders()
        signals = fetch_signals()
        for symbol in signals.get("buy", []):
            try:
                execute_signal_buy(symbol)
            except Exception as e:
                logger.error(f"Error on signal buy for {symbol}: {e}")
                send_alert(f"Error on signal buy for {symbol}: {e}", "error")
        for symbol in signals.get("sell", []):
            try:
                execute_signal_sell(symbol)
            except Exception as e:
                logger.error(f"Error on signal sell for {symbol}: {e}")
                send_alert(f"Error on signal sell for {symbol}: {e}", "error")
    except Exception as e:
        logger.error(f"Signal fetch failed: {e}")
        send_alert(f"Signal fetch failed: {e}", "error")


def ta_job():
    """TA-based trading loop over WATCH_SYMBOLS."""
    if not is_market_open():
        logger.info("Market closed - skipping")
        return
    reconcile_pending_orders()
    no_signal = []
    for symbol in _ta_symbols():
        try:
            fetch_and_store(symbol)
            analysis = analyze_trends(symbol)
            result = execute_trade(symbol, analysis)
            if result:
                no_signal.append(result)
        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            send_alert(f"Error processing {symbol}: {e}", "error")
    if no_signal:
        lines = "\n".join(f"• {s}" for s in no_signal)
        send_alert(f"No signal this cycle:\n{lines}", "hodl")
    try:
        prune_old_trends()
        prune_old_trade_log()
    except Exception as e:
        logger.warning(f"Prune failed: {e}")
        send_alert(f"Prune failed: {e}", "error")

def open_snapshot_job():
    """Capture market-open portfolio equity once per trading day."""
    if not is_market_open():
        return
    try:
        equity = snapshot_portfolio("open")
        if equity is not None:
            logger.info(f"Market-open snapshot: ${equity:,.2f}")
    except Exception as e:
        logger.error(f"Open snapshot failed: {e}")
        send_alert(f"Open snapshot failed: {e}", "error")


def eod_job():
    """After market close, capture closing equity and send the daily Discord summary (once per day)."""
    now_et = datetime.datetime.now(_ET)
    if now_et.weekday() >= 5 or now_et.hour < 16:
        return
    try:
        equity = snapshot_portfolio("close")
    except Exception as e:
        logger.error(f"EOD snapshot failed: {e}")
        send_alert(f"EOD snapshot failed: {e}", "error")
        return
    if equity is not None:
        logger.info(f"Market-close snapshot: ${equity:,.2f}")
        try:
            send_eod_summary()
        except Exception as e:
            logger.error(f"EOD summary failed: {e}")
            send_alert(f"EOD summary failed: {e}", "error")


if __name__ == "__main__":
    # Initialize database schema up front so tables exist before first run.
    try:
        init_db()
    except Exception as e:
        # If DB init fails, alert and stop; running without tables is useless.
        send_alert(f"Database initialization failed: {e}", "error")
        raise

    schedule.every(FMP_CHECK_INTERVAL_MINUTES).minutes.do(fmp_job)
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(ta_job)
    schedule.every(15).minutes.do(open_snapshot_job)
    schedule.every(15).minutes.do(eod_job)

    logger.info(
        f"Trading bot started | mode={TRADING_MODE.upper()} | symbols={SYMBOLS} | "
        f"ta_every={CHECK_INTERVAL_MINUTES}m fmp_every={FMP_CHECK_INTERVAL_MINUTES}m | "
        f"daily_cap={MAX_DAILY_TRADES} weekly_cap={MAX_WEEKLY_TRADES} "
        f"max_positions={MAX_OPEN_POSITIONS} max_portfolio_risk={MAX_PORTFOLIO_RISK_PCT:.1%} "
        f"max_corr={MAX_POSITION_CORRELATION:.2f} max_slippage={MAX_SLIPPAGE_PCT:.2%} | "
        f"stop={STOP_LOSS_PCT:.0%} trail_activate={TRAIL_ACTIVATION_PCT:.0%} trail={TRAIL_PCT:.0%} | "
        f"notional=${NOTIONAL_PER_TRADE} adx_threshold={ADX_STRONG_TREND_THRESHOLD} | "
        f"mean_reversion={'ON' if MEAN_REVERSION_ENABLED else 'off'}"
    )
    # Run both jobs immediately so we see activity right away (e.g. in Railway logs).
    fmp_job()
    ta_job()

    while True:
        schedule.run_pending()
        time.sleep(1)