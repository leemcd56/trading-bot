# analysis.py
import time
import duckdb
import pandas as pd
import talib
import numpy as np
from config import DB_PATH
from data_providers import get_intraday_price
from utils import logger

# Don't trade on data older than this (minutes).
# With daily candles, allow up to ~7 days to account for weekends/holidays/provider delays.
STALE_BAR_MINUTES = 60 * 24 * 7


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
    try:
        df = con.execute(query).fetchdf()
    except Exception as e:
        # Handle case where the trends table doesn't exist yet (fresh DB / failed fetch).
        logger.warning(f"No trends data available for {symbol} (table missing or unreadable): {e}")
        if connection is None:
            con.close()
        return None
    if connection is None:
        con.close()

    if len(df) < 50:
        logger.warning(f"Not enough data for {symbol} (have {len(df)} bars)")
        return None

    # Data quality: skip if latest bar is too old (production only)
    if connection is None:
        try:
            last_ts = int(df["timestamp"].iloc[-1])
            if time.time() - last_ts > STALE_BAR_MINUTES * 60:
                logger.warning(f"Stale data for {symbol} (latest bar {STALE_BAR_MINUTES}+ min old)")
                return None
        except (ValueError, TypeError):
            pass

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
    df['ATR_14'] = talib.ATR(df['high'].values, df['low'].values, df['close'].values, timeperiod=14)

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
    if len(df) >= 2:
        yesterday_close = df["close"].iloc[-2]
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

    # ─── Longer-term risk: avoid buying when situation is clearly risky ───
    lookback = 80
    if len(df) >= lookback:
        window_high = float(df["high"].iloc[-lookback:].max())
        window_low = float(df["low"].iloc[-lookback:].min())
        low_pos = int(np.argmin(df["low"].iloc[-lookback:].values))
        drop_pct = (window_high - window_low) / window_high if window_high > 0 else 0
        bounce_pct = (close - window_low) / window_low if window_low > 0 else 0
        # Dead-cat bounce: sharp drop (>5%) then bounce (1–15% from low) with low in recent half of window
        low_is_recent = low_pos >= lookback // 2
        dead_cat_bounce = (
            drop_pct > 0.05
            and 0.01 < bounce_pct < 0.15
            and low_is_recent
        )
    else:
        dead_cat_bounce = False

    # Extended decline: price still >7% below 50-bar high (catching a falling knife)
    if len(df) >= 50:
        period_high_50 = float(df["high"].iloc[-50:].max())
        extended_decline = period_high_50 > 0 and close < period_high_50 * 0.93
    else:
        extended_decline = False

    # Volatility spike: recent ATR much higher than prior period → risky to enter
    if len(df) >= 28 and "ATR_14" in df.columns:
        atr_series = df["ATR_14"].dropna()
        if len(atr_series) >= 28:
            atr_recent = float(atr_series.iloc[-1])
            atr_older = float(atr_series.iloc[-28:-14].mean())
            volatility_spike = atr_older > 0 and atr_recent > 1.5 * atr_older
        else:
            volatility_spike = False
    else:
        volatility_spike = False

    avoid_long = dead_cat_bounce or extended_decline or volatility_spike

    # ATR for position sizing (stop distance)
    atr_14 = latest.get("ATR_14") if "ATR_14" in df.columns else None
    atr_14_float = float(atr_14) if _ok(atr_14) else None

    # Try to override close with a fresher intraday price when available
    intraday_price = get_intraday_price(symbol)
    current_price = float(intraday_price) if intraday_price and intraday_price > 0 else close

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
        "dead_cat_bounce": dead_cat_bounce,
        "extended_decline": extended_decline,
        "volatility_spike": volatility_spike,
        "avoid_long": avoid_long,
        "current_price": current_price,
        "atr_14": atr_14_float,
    }