"""Tests for data/features.py (STEP 4d PART 3). Synthetic data only."""
import pytest

from data.candles import resample_ohlcv
from data.features import FEATURE_KEYS, build_candle_sets, compute_features


def make_1m(n=960, start_close=100.0, drift=0.1, v=10.0, last_bucket_v=None):
    """n one-minute candles, epoch-aligned to 15m buckets."""
    base = 1700000000 - (1700000000 % 900)
    candles = []
    for i in range(n):
        c = start_close + drift * i
        vol = float(last_bucket_v if (last_bucket_v is not None and i >= n - 15) else v)
        candles.append({"t": base + i * 60, "o": c - 0.5, "h": c + 1.0,
                        "l": c - 1.0, "c": c, "v": vol})
    return candles


def make_1h(n=72, start_close=100.0, drift=5.0):
    base = 1700000000 - (1700000000 % 14400)
    return [{"t": base + i * 3600, "o": start_close + drift * i,
             "h": start_close + drift * i + 10, "l": start_close + drift * i - 10,
             "c": start_close + drift * i, "v": 100.0} for i in range(n)]


DEPTH = {
    "bids": [(100.0, 3.0, 300.0), (99.0, 2.0, 198.0)],
    "asks": [(101.0, 1.0, 101.0), (102.0, 1.0, 102.0)],
}


class TestBuildCandleSets:
    def test_15m_from_1m_and_4h_from_1h(self):
        sets = build_candle_sets(make_1m(960), make_1h(72))
        assert len(sets["15m"]) == 64          # 960 minutes / 15
        assert len(sets["1h"]) == 72           # passthrough
        assert len(sets["4h"]) == 18           # 72 hours / 4

    def test_resampled_shapes_match_client(self):
        sets = build_candle_sets(make_1m(60), make_1h(8))
        for c in sets["15m"] + sets["4h"]:
            assert set(c.keys()) == {"t", "o", "h", "l", "c", "v"}

    def test_empty_inputs(self):
        sets = build_candle_sets([], [])
        assert sets["15m"] == [] and sets["1h"] == [] and sets["4h"] == []


class TestComputeFeatures:
    def test_all_keys_present_long_history(self):
        out = compute_features(make_1m(960), make_1h(72), DEPTH)
        assert set(out.keys()) == set(FEATURE_KEYS)
        assert out["close_15m"] == pytest.approx(100.0 + 0.1 * 959)
        assert out["ema20_15m"] is not None
        assert out["ema50_15m"] is not None
        assert out["rsi14_15m"] is not None
        assert out["atr14_15m"] is not None
        assert out["atr_pct_15m"] is not None
        assert out["rel_volume_15m"] is not None
        assert out["vol_regime_15m"] in {"low", "normal", "high"}

    def test_uptrend_ema_order_and_rsi(self):
        out = compute_features(make_1m(960), make_1h(72), DEPTH)
        # steady rise: EMA20 above EMA50, RSI at the ceiling (no down moves)
        assert out["ema20_15m"] > out["ema50_15m"]
        assert out["rsi14_15m"] == 100.0

    def test_atr_and_atr_pct_math(self):
        out = compute_features(make_1m(960), make_1h(72), DEPTH)
        # resampled 15m bucket: h = c_last+1, l = c_first-1, and the 15-minute
        # trend is 14*0.1 -> h-l = 2 + 1.4 = 3.4 exactly, every bucket
        assert out["atr14_15m"] == pytest.approx(3.4, abs=1e-6)
        assert out["atr_pct_15m"] == pytest.approx(3.4 / (100.0 + 0.1 * 959), abs=1e-6)

    def test_relative_volume(self):
        out = compute_features(make_1m(960, last_bucket_v=20.0), make_1h(72), DEPTH)
        # last 15m bucket sums 15*20=300; previous buckets 15*10=150 -> 2.0
        assert out["rel_volume_15m"] == pytest.approx(2.0)

    def test_flat_series(self):
        out = compute_features(make_1m(960, drift=0.0), make_1h(72), DEPTH)
        assert out["rsi14_15m"] == 50.0        # no movement at all
        assert out["atr14_15m"] == pytest.approx(2.0)  # h-l still spans 2
        assert out["vol_regime_15m"] in {"low", "normal", "high"}

    def test_declining_series_regime_high(self):
        # atr_pct = 2/close SHRINKS as close rises -> last is smallest -> low.
        # Invert: falling closes make atr_pct GROW -> last is largest -> high.
        out = compute_features(make_1m(960, start_close=200.0, drift=-0.1), make_1h(72), DEPTH)
        assert out["vol_regime_15m"] == "high"

    def test_rising_series_regime_low(self):
        out = compute_features(make_1m(960), make_1h(72), DEPTH)
        assert out["vol_regime_15m"] == "low"

    def test_spread_pct(self):
        out = compute_features(make_1m(960), make_1h(72), DEPTH)
        # (101-100)/100.5
        assert out["spread_pct"] == pytest.approx(1.0 / 100.5, abs=1e-6)

    def test_book_imbalance_top10(self):
        out = compute_features(make_1m(960), make_1h(72), DEPTH)
        # bids 3+2=5, asks 1+1=2 -> (5-2)/(5+2)
        assert out["book_imbalance_top10"] == pytest.approx(3.0 / 7.0, abs=1e-6)

    def test_empty_inputs_all_none(self):
        out = compute_features([], [], None)
        assert set(out.keys()) == set(FEATURE_KEYS)
        for key in FEATURE_KEYS:
            assert out[key] is None

    def test_short_series_gives_none_indicators(self):
        # 60 1m candles -> 4 buckets: close known, everything else too short
        out = compute_features(make_1m(60), make_1h(2), DEPTH)
        assert out["close_15m"] is not None
        assert out["ema20_15m"] is None
        assert out["ema50_15m"] is None
        assert out["rsi14_15m"] is None
        assert out["atr14_15m"] is None
        assert out["atr_pct_15m"] is None
        assert out["vol_regime_15m"] is None
        assert out["spread_pct"] is not None     # book features independent
        # 4 equal-volume buckets -> relative volume is a clean 1.0
        assert out["rel_volume_15m"] == pytest.approx(1.0)

    def test_rel_volume_none_with_single_bucket(self):
        out = compute_features(make_1m(15), [], DEPTH)  # exactly one 15m bucket
        assert out["close_15m"] is not None
        assert out["rel_volume_15m"] is None

    def test_rel_volume_needs_two_buckets(self):
        out = compute_features(make_1m(30), [], DEPTH)  # 2 buckets
        assert out["rel_volume_15m"] is not None

    def test_never_raises_on_garbage(self):
        out = compute_features([{"t": 1}], [{"bad": 1}], {"bids": None, "asks": "x"})
        assert out["close_15m"] is None
        assert out["spread_pct"] is None
        assert out["book_imbalance_top10"] is None

    def test_one_sided_book(self):
        out = compute_features([], [], {"bids": [(100.0, 1.0, 100.0)], "asks": []})
        assert out["spread_pct"] is None
        assert out["book_imbalance_top10"] is None

    def test_rounded_floats(self):
        out = compute_features(make_1m(960), make_1h(72), DEPTH)
        for key in FEATURE_KEYS:
            v = out[key]
            if isinstance(v, float):
                assert v == round(v, 6)
