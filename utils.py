import datetime
import pytz
import logging
import os
from dotenv import load_dotenv

load_dotenv()

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("logs/bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def is_market_open():
    now = datetime.datetime.now(pytz.timezone('US/Eastern'))
    if now.weekday() >= 5:
        return False
    open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_time <= now <= close_time