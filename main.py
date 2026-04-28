import time
import schedule
from data_fetch import fetch_and_store, prune_old_trends
from analysis import analyze_trends
from trading import execute_trade, execute_signal_buy, execute_signal_sell, prune_old_trade_log
from migrations import init_db
from signals import fetch_signals
from utils import logger, is_market_open
from alerts import send_alert
from config import SYMBOLS, CHECK_INTERVAL_MINUTES, FMP_CHECK_INTERVAL_MINUTES


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
    for symbol in SYMBOLS:
        try:
            fetch_and_store(symbol)
            analysis = analyze_trends(symbol)
            execute_trade(symbol, analysis)
        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            send_alert(f"Error processing {symbol}: {e}", "error")
    try:
        prune_old_trends()
        prune_old_trade_log()
    except Exception as e:
        logger.warning(f"Prune failed: {e}")
        send_alert(f"Prune failed: {e}", "error")

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

    logger.info("Trading bot started...")
    # Run both jobs immediately so we see activity right away (e.g. in Railway logs).
    fmp_job()
    ta_job()

    while True:
        schedule.run_pending()
        time.sleep(1)