import pandas as pd

from data_fetch import _normalize_timestamps


def test_normalize_timestamps_drops_invalid_values():
    df = pd.DataFrame(
        {
            "symbol": ["TEST", "TEST", "TEST", "TEST"],
            "timestamp": ["1711929600", "oops", None, 1],
            "open": [1.0, 1.0, 1.0, 1.0],
            "high": [1.0, 1.0, 1.0, 1.0],
            "low": [1.0, 1.0, 1.0, 1.0],
            "close": [1.0, 1.0, 1.0, 1.0],
            "volume": [100.0, 100.0, 100.0, 100.0],
        }
    )

    out = _normalize_timestamps(df, "TEST")

    assert len(out) == 1
    assert int(out["timestamp"].iloc[0]) == 1711929600
    assert str(out["timestamp"].dtype) == "int64"
