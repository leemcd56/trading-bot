import time
import schedule
from data_fetch import fetch_and_store
from analysis import analyze_trends
from trading import execute_trade
from utils import logger, is_market_open
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

schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job)

logger.info("Trading bot started...")
while True:
    schedule.run_pending()
    time.sleep(1)