"""Feature computation for the model input (pure, pandas).

Inputs are the RAW candle fetches: 1-minute candles from
WallexClient.get_candles_1m (resolution "15" really returns 1m data -- see
docs/wallex_api.md) and hourly candles from get_candles_1h (resolution "60").
The 15m and 4h series are built here with data.candles.resample_ohlcv,
because the exchange has no real 15m/4h resolutions ("240"/"720" are aliases
of "60").

compute_features() returns a plain dict of rounded floats (vol_regime_15m is
a string). Short or empty series give None fields; the function NEVER raises.
"""
from typing import Any, Dict, List, Optional

import pandas as pd

from data.candles import resample_ohlcv

FEATURE_KEYS = (
    "close_15m", "ema20_15m", "ema50_15m", "rsi14_15m",
    "atr14_15m", "atr_pct_15m", "rel_volume_15m", "vol_regime_15m",
    "spread_pct", "book_imbalance_top10",
)

# minimum history (in 15m buckets) before a statistic is reported
_MIN_EMA20 = 20
_MIN_EMA50 = 50
_MIN_RSI = 15
_MIN_ATR = 15
_MIN_REGIME = 20
_REL_VOLUME_LOOKBACK = 20


def build_candle_sets(candles_1m: List[Dict[str, Any]],
                      candles_1h: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Build the series the prompt needs: 15m resampled from 1m, 4h from 1h.

    The 1h set is passed through untouched. The 15m/4h sets drop the last
    (possibly incomplete) bucket by default so indicators never see a partial
    candle.
    """
    return {
        "15m": resample_ohlcv(candles_1m, 15),
        "1h": list(candles_1h or []),
        "4h": resample_ohlcv(candles_1h, 240),
    }


def _r6(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6)


def _ema_last(series: pd.Series, span: int) -> Optional[float]:
    if len(series) < span:
        return None
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


def _rsi14(close: pd.Series) -> Optional[float]:
    """Wilder's RSI(14) on a close series; None if too short."""
    if len(close) < _MIN_RSI:
        return None
    delta = close.diff().dropna()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_gain = float(up.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    avg_loss = float(down.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _atr14(df: pd.DataFrame) -> Optional[float]:
    """Wilder's ATR(14) of the last complete bucket; None if too short."""
    if len(df) < _MIN_ATR:
        return None
    high, low, close = df["h"], df["l"], df["c"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    tr = tr.dropna()
    if len(tr) == 0:
        return None
    return float(tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1])


def _atr14_series(df: pd.DataFrame) -> Optional[pd.Series]:
    """ATR(14) as a full series (for the volatility-regime percentile)."""
    if len(df) < _MIN_ATR:
        return None
    high, low, close = df["h"], df["l"], df["c"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / 14, adjust=False).mean()


def _rel_volume(volume: pd.Series) -> Optional[float]:
    """Last bucket volume over the mean of the previous ones."""
    if len(volume) < 2:
        return None
    prev = volume.iloc[:-1].tail(_REL_VOLUME_LOOKBACK)
    mean = float(prev.mean())
    if mean <= 0:
        return None
    return float(volume.iloc[-1]) / mean


def _vol_regime(df: pd.DataFrame) -> Optional[str]:
    """low/normal/high by the percentile of the latest ATR% in its own history."""
    atr = _atr14_series(df)
    if atr is None:
        return None
    atr_pct = (atr / df["c"].reindex(atr.index)).dropna()
    if len(atr_pct) < _MIN_REGIME:
        return None
    last = atr_pct.iloc[-1]
    percentile = float((atr_pct < last).mean())
    if percentile < 1 / 3:
        return "low"
    if percentile > 2 / 3:
        return "high"
    return "normal"


def _book_features(depth: Optional[Dict[str, Any]]) -> tuple[Optional[float], Optional[float]]:
    """spread_pct and top-10 imbalance from a get_depth() result (never raises)."""
    try:
        if not depth:
            return None, None
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if not bids or not asks:
            return None, None
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread = (best_ask - best_bid) / mid if mid > 0 else None

        bid_qty = sum(float(level[1]) for level in bids[:10])
        ask_qty = sum(float(level[1]) for level in asks[:10])
        total = bid_qty + ask_qty
        imbalance = (bid_qty - ask_qty) / total if total > 0 else None
        return spread, imbalance
    except Exception:
        return None, None


def compute_features(candles_1m: List[Dict[str, Any]],
                     candles_1h: List[Dict[str, Any]],
                     depth: Optional[Dict[str, Any]] = None) -> Dict[str, Optional[float]]:
    """Indicator dict for the model input. Never raises; short/empty series
    give None fields. All floats are rounded to 6 decimals."""
    out: Dict[str, Optional[float]] = {k: None for k in FEATURE_KEYS}

    try:
        c15 = build_candle_sets(candles_1m, candles_1h)["15m"]
        if c15:
            df = pd.DataFrame(c15)
            close = df["c"]
            out["close_15m"] = _r6(close.iloc[-1])
            out["ema20_15m"] = _r6(_ema_last(close, _MIN_EMA20))
            out["ema50_15m"] = _r6(_ema_last(close, _MIN_EMA50))
            out["rsi14_15m"] = _r6(_rsi14(close))
            atr = _atr14(df)
            out["atr14_15m"] = _r6(atr)
            if atr is not None and float(close.iloc[-1]) > 0:
                out["atr_pct_15m"] = _r6(atr / float(close.iloc[-1]))
            out["rel_volume_15m"] = _r6(_rel_volume(df["v"]))
            try:
                out["vol_regime_15m"] = _vol_regime(df)
            except Exception:
                out["vol_regime_15m"] = None
    except Exception:
        pass

    spread, imbalance = _book_features(depth)
    out["spread_pct"] = _r6(spread)
    out["book_imbalance_top10"] = _r6(imbalance)
    return out
