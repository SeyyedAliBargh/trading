"""Tests for the pure sizing in scripts/probe_orders.py (STEP 4d PART 0.2)."""
import pytest

from scripts.probe_orders import size_limit_buy


class TestSizeLimitBuy:
    def test_xrp_like_precision_0_amount_1(self):
        # best_bid 30000, price_precision 0, amount_precision 1
        s = size_limit_buy(30000.0, 0, 1)
        assert s["price_str"] == "24000"
        assert s["quantity_str"] == "4.1"
        assert s["notional"] == pytest.approx(24000 * 4.1)
        assert s["ok"] is True

    def test_btc_like_price_has_no_decimals(self):
        # the real 422 case: price must have 0 decimals; amount_precision 8
        s = size_limit_buy(17671321516.8, 0, 8)
        assert s["price_str"] == "14137057213"  # floor(0.8 * best_bid), no ".0"
        assert "." in s["quantity_str"]
        assert s["ok"] is True
        assert 50_000 <= s["notional"] <= 100_000

    def test_quantity_floored_at_amount_precision(self):
        s = size_limit_buy(1000.0, 0, 5)
        assert s["price_str"] == "800"
        # 100000/800 = 125 exactly
        assert s["quantity_str"] == "125.00000"
        assert s["notional"] == pytest.approx(100_000)
        assert s["ok"] is True

    def test_amount_precision_1_rounds_down(self):
        s = size_limit_buy(912.5, 0, 1)
        assert s["price_str"] == "730"
        # 100000/730 = 136.986... -> floor to 1 decimal
        assert s["quantity_str"] == "136.9"
        assert s["notional"] == pytest.approx(730 * 136.9)
        assert s["ok"] is True

    def test_notional_above_cap_aborts(self):
        # price so high that qty floors to 1 and the notional exceeds the cap
        s = size_limit_buy(200_000.0, 0, 0)
        assert s["price_str"] == "160000"
        assert s["quantity_str"] == "0"
        assert s["ok"] is False

    def test_notional_above_cap_with_qty_1(self):
        s = size_limit_buy(120_000.0, 0, 0, target_notional_tmn=100_000.0)
        assert s["quantity_str"] == "1"
        assert s["notional"] == pytest.approx(96_000)
        assert s["ok"] is True

    def test_notional_below_min(self):
        s = size_limit_buy(50_000.0, 0, 0, target_notional_tmn=45_000.0)
        assert s["notional"] == pytest.approx(40_000)
        assert s["ok"] is False
        assert "min" in s["reason"]

    def test_quantity_floors_to_zero(self):
        # 1M TMN per unit, integer amounts only -> qty floors to 0
        s = size_limit_buy(1_000_000.0, 0, 0)
        assert s["quantity_str"] == "0"
        assert s["ok"] is False
        assert s["reason"]

    def test_invalid_best_bid(self):
        s = size_limit_buy(0.0, 0, 1)
        assert s["ok"] is False
        assert s["reason"]

    def test_never_raises_on_garbage(self):
        s = size_limit_buy(None, None, None)
        assert s["ok"] is False
        assert s["reason"]
