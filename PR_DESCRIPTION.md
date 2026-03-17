# Fix analysis window and null OHLC in trends data

## Problem

1. **Analysis used the wrong 300 bars**  
   The query `ORDER BY timestamp ASC LIMIT 300` returned the *oldest* 300 bars. The "latest" bar in that set was ~300 days old, so the staleness check always failed (latest bar &gt; 7 days) and the bot logged "Stale data for SYMBOL (latest bar 10080+ min old)" and "Skipping - no strong trend" every run. Indicators were also computed on old history instead of the most recent data.

2. **All OHLC columns were null in `trends`**  
   Every row had `symbol` and `timestamp` populated but `open`, `high`, `low`, `close` were null and `volume` was 0. The Yahoo Finance (yfinance) path was building a DataFrame with column names that can vary by version (e.g. `"Datetime"` vs `"Date"`, MultiIndex columns, or casing), so OHLC wasn’t mapped correctly and we were writing nulls.

## Solution

### analysis.py

- Load the **most recent** 300 bars: `ORDER BY timestamp DESC LIMIT 300`.
- After fetching, reverse the DataFrame (`df.iloc[::-1].reset_index(drop=True)`) so TA-Lib still receives chronological order.
- The "latest" bar is now the actual last bar in the DB; staleness only triggers when data is genuinely old.

### data_providers.py

- Add **`_col(df, *candidates)`** to resolve columns case-insensitively and support MultiIndex (uses first level).
- **`_fetch_from_yahoo_daily`** uses `_col()` for the date column (`"Date"`, `"Datetime"`, `"index"`) and for OHLCV (`"Open"`/`"open"`, etc.), uses `pd.to_numeric(..., errors="coerce")`, and **drops rows with null `close`** so we never return all-null OHLC.
- Log clear warnings when date/close columns are missing or there is no valid OHLC.

### data_fetch.py

- Before upserting, **refuse to store** when the new DataFrame has all-null `close` (log and return). Prevents writing bad data even if a provider returns it.

## Follow-up for deploy

After merging, clear existing bad rows and re-fetch:

```bash
python -c "
import duckdb
from config import DB_PATH
con = duckdb.connect(DB_PATH)
con.execute('DELETE FROM trends')
print('Cleared trends table.')
con.close()
"
# Then run the bot / fetch again
```

## Testing

- [ ] Run `fetch_and_store(symbol)` for a symbol and confirm `trends` has non-null `open`/`high`/`low`/`close` and non-zero `volume` where expected.
- [ ] Run the bot; confirm "Stale data" no longer appears when the last bar is within 7 days (e.g. last trading day).
- [ ] Confirm analysis uses recent data (e.g. check that last bar timestamp in analysis is the latest in DB).
