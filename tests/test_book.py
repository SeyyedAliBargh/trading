"""Tests for exchange/book.py (pure functions)."""
import pytest
from exchange.book import fill_estimate, fill_qty, entry_limit_price


class TestFillEstimate:
    def test_single_level_enough(self):
        levels = [(100.0, 10.0, 1000.0)]
        est = fill_estimate(levels, 500.0)
        assert est["enough_depth"] is True
        assert est["worst_price"] == 100.0
        assert est["qty"] == 5.0
        assert est["filled_notional"] == 500.0
        assert est["vwap"] == 100.0

    def test_multi_level(self):
        levels = [(100.0, 1.0, 100.0), (101.0, 2.0, 202.0), (102.0, 5.0, 510.0)]
        est = fill_estimate(levels, 100.0 + 202.0 + 102.0)  # 404
        assert est["enough_depth"] is True
        assert est["worst_price"] == 102.0
        assert est["qty"] == pytest.approx(1.0 + 2.0 + 1.0)
        assert est["vwap"] == pytest.approx(404.0 / 4.0)

    def test_insufficient_depth(self):
        levels = [(100.0, 1.0, 100.0)]
        est = fill_estimate(levels, 1000.0)
        assert est["enough_depth"] is False
        assert est["filled_notional"] == 100.0

    def test_empty_book(self):
        est = fill_estimate([], 100.0)
        assert est["enough_depth"] is False
        assert est["qty"] == 0.0


class TestFillQty:
    def test_walks_levels(self):
        levels = [(100.0, 1.0, 100.0), (101.0, 2.0, 202.0)]
        res = fill_qty(levels, 2.5)
        assert res["filled_qty"] == 2.5
        assert res["avg_price"] == pytest.approx((100.0 + 101.0 * 1.5) / 2.5)

    def test_price_cap(self):
        levels = [(100.0, 1.0, 100.0), (101.0, 5.0, 505.0)]
        res = fill_qty(levels, 3.0, max_price=100.0)
        assert res["filled_qty"] == 1.0
        assert res["enough_depth"] is False

    def test_min_price_sell_floor(self):
        # STEP P-B regression: a marketable LIMIT SELL (price <= best bid)
        # must fill against bids at or above the limit
        levels = [(100.0, 5.0, 500.0), (98.0, 5.0, 490.0)]
        res = fill_qty(levels, 2.0, min_price=97.0)
        assert res["filled_qty"] == 2.0
        assert res["avg_price"] == pytest.approx(100.0)

    def test_min_price_skips_bids_below_limit(self):
        # bids below the sell limit must NOT fill the order
        levels = [(99.0, 5.0, 495.0), (98.0, 5.0, 490.0)]
        res = fill_qty(levels, 2.0, min_price=101.0)
        assert res["filled_qty"] == 0.0
        assert res["enough_depth"] is False


class TestEntryLimitPrice:
    def test_within_slippage(self):
        asks = [(100.0, 5.0, 500.0), (100.5, 5.0, 502.5)]
        p = entry_limit_price(asks, 1000.0, 0.01, 0, 0)
        assert p == 100.5

    def test_slippage_cap_exceeded(self):
        asks = [(100.0, 1.0, 100.0), (200.0, 100.0, 20000.0)]
        assert entry_limit_price(asks, 1000.0, 0.003, 0, 0) is None

    def test_insufficient_depth_returns_none(self):
        asks = [(100.0, 1.0, 100.0)]
        assert entry_limit_price(asks, 1e9, 0.01, 0, 0) is None

    def test_ticks_above(self):
        asks = [(100.0, 10.0, 1000.0)]
        p = entry_limit_price(asks, 500.0, 0.01, 2, 2)
        assert p == pytest.approx(100.02)

    def test_thin_sol_like_book(self):
        # best ask notional ~0.19M TMN, order 1M TMN
        asks = [(100.0, 1900.0, 190000.0), (101.0, 100.0, 10100.0)]
        est = fill_estimate(asks, 1_000_000.0)
        assert est["enough_depth"] is False
        assert entry_limit_price(asks, 1_000_000.0, 0.003, 0, 0) is None
