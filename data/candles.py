"""Candle resampling helpers (pure, uses pandas)."""
from typing import List, Dict, Any
import pandas as pd

# Candle structure returned by WallexClient.get_candles():
#   {"t": int (epoch seconds), "o": float, "h": float, "l": float, "c": float, "v": float}
# resample_ohlcv returns the SAME structure: a list of such dicts.


def resample_ohlcv(candles: List[Dict[str, Any]], minutes: int,
                   drop_incomplete: bool = True) -> List[Dict[str, Any]]:
    """Bucket candles into `minutes`-sized epoch-aligned buckets.

    o=first, h=max, l=min, c=last, v=sum within each bucket.
    If drop_incomplete is True, the LAST bucket is dropped when the bucket's
    end extends beyond the last candle timestamp (i.e. the bucket is not
    fully covered by the data). Returns [] for empty input.
    """
    if not candles or minutes <= 0:
        return []
    df = pd.DataFrame(candles)
    if "t" not in df.columns or df.empty:
        return []
    df["t"] = df["t"].astype("int64")
    df = df.sort_values("t")
    bucket_sec = minutes * 60
    df["bucket"] = (df["t"] // bucket_sec) * bucket_sec

    last_bucket = int(df["bucket"].max())
    # expected candles per full bucket, derived from the most common gap
    ts = df["t"].tolist()
    if len(ts) >= 2:
        deltas = [b - a for a, b in zip(ts, ts[1:]) if b > a]
        mode_delta = max(set(deltas), key=deltas.count) if deltas else bucket_sec
    else:
        mode_delta = bucket_sec
    expected = max(1, bucket_sec // max(1, mode_delta))

    grouped = df.groupby("bucket")
    out: List[Dict[str, Any]] = []
    for bucket, g in grouped:
        if drop_incomplete and int(bucket) == last_bucket and len(g) < expected:
            continue
        out.append({
            "t": int(bucket),
            "o": float(g["o"].iloc[0]),
            "h": float(g["h"].max()),
            "l": float(g["l"].min()),
            "c": float(g["c"].iloc[-1]),
            "v": float(g["v"].sum()),
        })
    return out
