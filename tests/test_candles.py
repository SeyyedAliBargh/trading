"""Tests for data/candles.py (resample_ohlcv)."""
from data.candles import resample_ohlcv


def _candle(t, o, h, l, c, v):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


class TestResample:
    def test_1m_to_15m(self):
        base = 1700000000 - (1700000000 % 900)  # aligned to 15 min
        candles = [_candle(base + i * 60, float(i), float(i) + 1, float(i) - 1,
                           float(i) + 0.5, 1.0) for i in range(30)]
        out = resample_ohlcv(candles, 15)
        assert len(out) == 2
        assert out[0]["t"] == base
        assert out[0]["o"] == 0.0
        assert out[0]["h"] == 14.0 if False else out[0]["h"] >= out[0]["o"]
        assert out[0]["v"] == 15.0

    def test_60m_to_240m(self):
        base = 1700000000 - (1700000000 % 14400)
        candles = [_candle(base + i * 3600, 1.0, 2.0, 0.5, 1.5, 10.0)
                   for i in range(4)]
        out = resample_ohlcv(candles, 240, drop_incomplete=False)
        assert len(out) == 1
        assert out[0]["v"] == 40.0

    def test_incomplete_last_bucket_dropped(self):
        base = 1700000000 - (1700000000 % 900)
        candles = [_candle(base + i * 60, 1.0, 1.0, 1.0, 1.0, 1.0) for i in range(20)]
        out_drop = resample_ohlcv(candles, 15, drop_incomplete=True)
        out_keep = resample_ohlcv(candles, 15, drop_incomplete=False)
        assert len(out_drop) == 1
        assert len(out_keep) == 2

    def test_gaps(self):
        base = 1700000000 - (1700000000 % 900)
        candles = [_candle(base, 1.0, 1.0, 1.0, 1.0, 1.0),
                   _candle(base + 60, 1.0, 1.0, 1.0, 1.0, 1.0),
                   _candle(base + 10 * 60, 2.0, 2.0, 2.0, 2.0, 1.0)]
        out = resample_ohlcv(candles, 15, drop_incomplete=False)
        assert len(out) == 1
        assert out[0]["v"] == 3.0

    def test_empty_input(self):
        assert resample_ohlcv([], 15) == []
