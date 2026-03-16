import time
import schedule
from data_fetch import fetch_and_store, prune_old_trends
from analysis import analyze_trends
from trading import execute_trade, prune_old_trade_log
from migrations import init_db
from utils import logger, is_market_open
from alerts import send_alert
from config import SYMBOLS, CHECK_INTERVAL_MINUTES

def job():
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

    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job)

    logger.info("Trading bot started...")
    # Run first cycle immediately so we see activity right away (e.g. in Railway logs).
    job()

    while True:
        schedule.run_pending()
        time.sleep(1)