# analysis.py
import duckdb
import pandas as pd
import talib
import numpy as np
from config import DB_PATH
from utils import logger


def analyze_trends(symbol: str, connection=None) -> dict | None:
    """
    Analyze trend signals for a symbol from the trends table.
    connection: optional DuckDB connection (e.g. for tests with spoofed data).
    """
    con = connection if connection is not None else duckdb.connect(DB_PATH)
    query = f"""
        SELECT * FROM trends
        WHERE symbol = '{symbol}'
        ORDER BY timestamp ASC
        LIMIT 300
    """
    df = con.execute(query).fetchdf()
    if connection is None:
        con.close()

    if len(df) < 50:
        logger.warning(f"Not enough data for {symbol} (have {len(df)} bars)")
        return None

    # Ensure numeric columns
    for col in ['high', 'low', 'close']:
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Bollinger Bands
    upper, middle, lower = talib.BBANDS(
        df['close'].values, timeperiod=20, nbdevup=2.0, nbdevdn=2.0, matype=0
    )
    df['BB_upper'] = upper
    df['BB_middle'] = middle
    df['BB_lower'] = lower

    # Parabolic SAR
    df['SAR'] = talib.SAR(df['high'].values, df['low'].values, acceleration=0.02, maximum=0.20)

    # ADX / DI
    df['ADX'] = talib.ADX(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
    df['+DI'] = talib.PLUS_DI(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)
    df['-DI'] = talib.MINUS_DI(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)

    # RSI, MACD, SMA
    df['RSI_14'] = talib.RSI(df['close'], timeperiod=14)
    macd, signal, _ = talib.MACD(df['close'], fastperiod=12, slowperiod=26, signalperiod=9)
    df['MACD'] = macd
    df['MACD_signal'] = signal
    df['SMA_50'] = talib.SMA(df['close'], timeperiod=50)

    latest = df.iloc[-1]
    previous = df.iloc[-2] if len(df) >= 2 else None

    def _ok(x):
        if x is None or (isinstance(x, float) and np.isnan(x)):
            return False
        return True

    # Trend strength and direction
    adx = latest.get("ADX") if hasattr(latest, "get") else latest["ADX"]
    plus_di = latest["+DI"]
    minus_di = latest["-DI"]
    strong_trend = _ok(adx) and float(adx) > 25
    uptrend = _ok(plus_di) and _ok(minus_di) and float(plus_di) > float(minus_di)

    # Parabolic SAR vs price
    close = float(latest["close"])
    sar = latest["SAR"]
    sar_below_price = _ok(sar) and close > float(sar)
    sar_above_price = _ok(sar) and close < float(sar)

    # SAR flip detection (previous bar vs latest)
    sar_flipped_to_bull = False
    sar_flipped_to_bear = False
    if previous is not None and _ok(sar):
        prev_close = float(previous["close"])
        prev_sar = previous["SAR"]
        if _ok(prev_sar):
            if prev_close < float(prev_sar) and close > float(sar):
                sar_flipped_to_bull = True
            if prev_close > float(prev_sar) and close < float(sar):
                sar_flipped_to_bear = True

    # Bollinger Bands
    bb_upper = latest["BB_upper"]
    bb_lower = latest["BB_lower"]
    bb_middle = latest["BB_middle"]
    near_upper_band = _ok(bb_upper) and close >= float(bb_upper) * 0.995
    near_lower_band = _ok(bb_lower) and close <= float(bb_lower) * 1.005
    bb_width_pct = (
        (float(bb_upper) - float(bb_lower)) / float(bb_middle)
        if _ok(bb_middle) and float(bb_middle) != 0
        else 1.0
    )
    bb_squeeze = bb_width_pct < 0.04

    # +DI / -DI crossovers
    bullish_crossover = False
    bearish_crossover = False
    if previous is not None:
        prev_plus = previous["+DI"]
        prev_minus = previous["-DI"]
        if _ok(prev_plus) and _ok(prev_minus) and _ok(plus_di) and _ok(minus_di):
            if float(prev_plus) <= float(prev_minus) and float(plus_di) > float(minus_di):
                bullish_crossover = True
            if float(prev_plus) >= float(prev_minus) and float(plus_di) < float(minus_di):
                bearish_crossover = True

    # Composite: trending up with momentum (AGENTS.md buy condition)
    rsi = latest["RSI_14"]
    macd = latest["MACD"]
    macd_sig = latest["MACD_signal"]
    sma50 = latest["SMA_50"]
    trending_up_a_lot = (
        strong_trend
        and uptrend
        and _ok(sma50)
        and close > float(sma50)
        and _ok(macd)
        and _ok(macd_sig)
        and float(macd) > float(macd_sig)
        and _ok(rsi)
        and float(rsi) > 55
    )

    # Similar to yesterday: |change vs prior day close| < 2%
    similar_to_yesterday = False
    if len(df) >= 1440:
        yesterday_close = df.iloc[-1440]["close"]
        if _ok(yesterday_close) and float(yesterday_close) != 0:
            pct_change = abs(close - float(yesterday_close)) / float(yesterday_close)
            similar_to_yesterday = pct_change < 0.02

    # Dive bombing: downtrend + RSI < 35 + sharp drop from recent high
    recent_high = float(df["high"].iloc[-10:].max()) if len(df) >= 10 else close
    pct_drop = (recent_high - close) / recent_high if recent_high != 0 else 0
    dive_bombing = (
        not uptrend
        and _ok(rsi)
        and float(rsi) < 35
        and pct_drop > 0.02
    )

    return {
        "strong_trend": strong_trend,
        "uptrend": uptrend,
        "sar_below_price": sar_below_price,
        "sar_above_price": sar_above_price,
        "sar_flipped_to_bull": sar_flipped_to_bull,
        "sar_flipped_to_bear": sar_flipped_to_bear,
        "near_upper_band": near_upper_band,
        "near_lower_band": near_lower_band,
        "bb_squeeze": bb_squeeze,
        "bullish_crossover": bullish_crossover,
        "bearish_crossover": bearish_crossover,
        "trending_up_a_lot": trending_up_a_lot,
        "similar_to_yesterday": similar_to_yesterday,
        "dive_bombing": dive_bombing,
        "current_price": close,
    }