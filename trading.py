# trading.py
import os
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv
from utils import logger

load_dotenv()

trading_client = TradingClient(
    api_key=os.getenv('ALPACA_API_KEY'),
    secret_key=os.getenv('ALPACA_SECRET_KEY'),
    paper=True   # Change to False only when going live (very carefully!)
)

def execute_trade(symbol: str, analysis: dict | None):
    if not analysis or not analysis.get('strong_trend', False):
        logger.info(f"{symbol}: Skipping - no strong trend")
        return

    # ────────────────────────────────────────────────────────────────
    # Your full decision logic with Bollinger, SAR, DI crossover, etc.
    # ────────────────────────────────────────────────────────────────

    if (
        analysis.get('trending_up_a_lot') and
        analysis.get('near_upper_band') and
        analysis.get('sar_below_price') and
        (analysis.get('bullish_crossover') or analysis.get('sar_flipped_to_bull')) and
        not analysis.get('similar_to_yesterday', False) and
        not analysis.get('bb_squeeze', False)
    ):
        order = MarketOrderRequest(
            symbol=symbol,
            qty=1,                  # keep small for testing!
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order)
        logger.info(f"BUY submitted for {symbol}")

    # Sell / exit logic (similar structure)
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
                logger.info(f"SELL submitted for {symbol}")
        except Exception as e:
            if "position does not exist" not in str(e).lower():
                logger.error(f"Position check failed for {symbol}: {e}")