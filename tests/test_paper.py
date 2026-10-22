"""Tests for exchange/paper.py (STEP 4b)."""
import pytest
from unittest.mock import MagicMock

import config
import store.db as store_db
from exchange.paper import PaperExchange, PaperExchangeError, PaperStatus


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "paper_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


def make_data_source(asks=None, bids=None, taker=0.002):
    ds = MagicMock()
    ds.get_depth.return_value = {
        "asks": asks if asks is not None else [(100.0, 10.0, 1000.0), (101.0, 5.0, 505.0)],
        "bids": bids if bids is not None else [(99.0, 8.0, 792.0), (98.0, 12.0, 1176.0)],
    }
    ds.get_fee_for.return_value = {"maker_rate": 0.001, "taker_rate": taker}
    return ds


def make_paper(temp_db, ds=None):
    return PaperExchange(config.CONFIG, temp_db, ds if ds is not None else make_data_source())


class TestPaperExchange:
    def test_buy_limit_below_best_ask_stays_open(self, temp_db):
        paper = make_paper(temp_db)
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 99.0)
        o = res["order"]
        assert o["status"] == PaperStatus.NEW
        assert o["executedQty"] == 0.0
        assert o["active"] is True

    def test_buy_limit_at_best_ask_fills(self, temp_db):
        paper = make_paper(temp_db)
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        o = res["order"]
        assert o["status"] == PaperStatus.FILLED
        assert o["executedQty"] == 1.0
        assert abs(o["executedPrice"] - 100.0) < 1e-6
        assert o["active"] is False

    def test_buy_limit_partial_fill(self, temp_db):
        paper = make_paper(temp_db)
        # total ask qty 15 (10@100 + 5@101); request 20 @ <=101
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 20.0, 101.0)
        o = res["order"]
        assert o["status"] == PaperStatus.PARTIALLY_FILLED
        assert o["executedQty"] == 15.0

    def test_market_sell_walks_bids(self, temp_db):
        ds = make_data_source()
        paper = make_paper(temp_db, ds)
        # give the wallet some BTC first
        acct = store_db.get_paper_account()
        assets = acct.get("assets", {})
        assets["BTC"] = {"free": 5.0, "locked": 0.0}
        store_db.set_paper_account(acct["tmn_balance"], assets)
        res = paper.create_order("BTCTMN", "SELL", "MARKET", 5.0)
        o = res["order"]
        assert o["status"] == PaperStatus.FILLED
        assert abs(o["executedPrice"] - 99.0) < 1e-6

    # ---- fees ---------------------------------------------------------- #
    def test_buy_fee_in_base(self, temp_db):
        ds = make_data_source(taker=0.01)
        paper = make_paper(temp_db, ds)
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 2.0, 100.0)
        # fee in base: credited BTC = 2.0 * (1 - 0.01) = 1.98
        bal = paper.get_balances()
        assert abs(bal["BTC"]["free"] - 1.98) < 1e-9

    def test_buy_fee_in_tmn_when_flag_off(self, temp_db):
        cfg = config.CONFIG
        cfg2 = config.build_config()
        # build a config with paper_buy_fee_in_base=False
        import dataclasses
        cfg3 = dataclasses.replace(cfg2, trading=dataclasses.replace(cfg2.trading, paper_buy_fee_in_base=False))
        ds = make_data_source(taker=0.01)
        paper = PaperExchange(cfg3, temp_db, ds)
        before = paper.get_balances()["TMN"]["free"]
        paper.create_order("BTCTMN", "BUY", "LIMIT", 2.0, 100.0)
        after = paper.get_balances()
        # cost 200 + fee 2 => TMN down by 202; BTC full 2.0
        assert abs(after["TMN"]["free"] - (before - 202.0)) < 1e-6
        assert abs(after["BTC"]["free"] - 2.0) < 1e-9

    def test_sell_fee_in_tmn(self, temp_db):
        ds = make_data_source(taker=0.01)
        paper = make_paper(temp_db, ds)
        acct = store_db.get_paper_account()
        assets = acct.get("assets", {})
        assets["BTC"] = {"free": 1.0, "locked": 0.0}
        store_db.set_paper_account(acct["tmn_balance"], assets)
        before = paper.get_balances()["TMN"]["free"]
        paper.create_order("BTCTMN", "SELL", "MARKET", 1.0)
        after = paper.get_balances()["TMN"]["free"]
        # proceeds 99, fee 0.99 -> +98.01
        assert abs(after - (before + 98.01)) < 1e-6

    def test_fallback_fee_when_get_fee_fails(self, temp_db):
        ds = make_data_source()
        ds.get_fee_for.side_effect = Exception("down")
        paper = make_paper(temp_db, ds)
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        # fallback fee 0.005 in base -> credited 0.995
        bal = paper.get_balances()
        assert abs(bal["BTC"]["free"] - 0.995) < 1e-9

    # ---- balance safety ------------------------------------------------- #
    def test_insufficient_balance_rejected(self, temp_db):
        ds = make_data_source(asks=[(100.0, 100000.0, 1e7)])
        paper = make_paper(temp_db, ds)
        with pytest.raises(PaperExchangeError):
            paper.create_order("BTCTMN", "BUY", "LIMIT", 100000.0, 100.0)

    def test_balances_never_negative(self, temp_db):
        paper = make_paper(temp_db)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 0.5, 100.0)
        bal = paper.get_balances()
        for v in bal.values():
            assert v["free"] >= -1e-9
            assert v["locked"] >= -1e-9

    def test_sell_full_executed_qty_rejected_after_buy_fee_in_base(self, temp_db):
        ds = make_data_source(taker=0.01)
        paper = make_paper(temp_db, ds)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 2.0, 100.0)
        # balance is 1.98, selling 2.0 must fail
        with pytest.raises(PaperExchangeError):
            paper.create_order("BTCTMN", "SELL", "MARKET", 2.0)
        # selling the actual balance succeeds
        res = paper.create_order("BTCTMN", "SELL", "MARKET", 1.98)
        assert res["order"]["status"] == PaperStatus.FILLED

    # ---- resting orders / check_open_orders ------------------------------ #
    def test_check_open_orders_fills_resting_buy(self, temp_db):
        ds = make_data_source()
        paper = make_paper(temp_db, ds)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 99.0)
        # now the ask drops to 98
        ds.get_depth.return_value = {"bids": [(97.0, 5.0, 485.0)],
                                     "asks": [(98.0, 10.0, 980.0)]}
        paper.check_open_orders()
        row = store_db.get_order_by_client_id(
            [r["client_id"] for r in store_db.get_orders_by_status([PaperStatus.FILLED], True)][0]
        )
        assert row["status"] == PaperStatus.FILLED

    def test_check_open_orders_fills_resting_sell(self, temp_db):
        ds = make_data_source()
        paper = make_paper(temp_db, ds)
        acct = store_db.get_paper_account()
        assets = acct.get("assets", {})
        assets["BTC"] = {"free": 1.0, "locked": 0.0}
        store_db.set_paper_account(acct["tmn_balance"], assets)
        paper.create_order("BTCTMN", "SELL", "LIMIT", 1.0, 101.0)
        # bid rises to 102
        ds.get_depth.return_value = {"bids": [(102.0, 5.0, 510.0)],
                                     "asks": [(103.0, 5.0, 515.0)]}
        paper.check_open_orders()
        rows = store_db.get_orders_by_status([PaperStatus.FILLED], True)
        assert rows and rows[0]["status"] == PaperStatus.FILLED

    def test_check_open_orders_partial(self, temp_db):
        ds = make_data_source()
        paper = make_paper(temp_db, ds)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 5.0, 99.0)
        ds.get_depth.return_value = {"bids": [(97.0, 5.0, 485.0)],
                                     "asks": [(98.0, 2.0, 196.0)]}
        paper.check_open_orders()
        rows = store_db.get_orders_by_status([PaperStatus.PARTIALLY_FILLED], True)
        assert rows and abs(rows[0]["filled_quantity"] - 2.0) < 1e-9

    def test_check_open_orders_no_fill_when_not_crossed(self, temp_db):
        ds = make_data_source()
        paper = make_paper(temp_db, ds)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 99.0)
        paper.check_open_orders()  # ask is 100, order at 99 -> no fill
        rows = store_db.get_orders_by_status([PaperStatus.NEW], True)
        assert rows

    def test_cancel_resting_order_releases_funds(self, temp_db):
        ds = make_data_source()
        paper = make_paper(temp_db, ds)
        before = paper.get_balances()["TMN"]["free"]
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 2.0, 99.0)
        cid = res["order"]["clientOrderId"]
        paper.cancel_order(cid)
        after = paper.get_balances()
        assert abs(after["TMN"]["free"] - before) < 1e-6
        assert after["TMN"]["locked"] == 0.0

    def test_cancel_partially_filled_order(self, temp_db):
        ds = make_data_source(asks=[(100.0, 1.0, 100.0), (101.0, 0.5, 50.5)])
        paper = make_paper(temp_db, ds)
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 3.0, 101.0)
        cid = res["order"]["clientOrderId"]
        assert res["order"]["status"] == PaperStatus.PARTIALLY_FILLED
        cancel = paper.cancel_order(cid)
        assert cancel["order"]["status"] == PaperStatus.CANCELED

    # ---- order shape ------------------------------------------------------ #
    def test_order_dict_shape(self, temp_db):
        paper = make_paper(temp_db)
        res = paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        o = res["order"]
        for key in ("active", "clientOrderId", "created_at", "executedQty",
                    "executedPrice", "executedSum", "fee", "fills", "origQty",
                    "price", "side", "status", "symbol", "type", "updated_at"):
            assert key in o
        assert isinstance(o["fills"], list)

    def test_get_balances_shape_matches_probe(self, temp_db):
        paper = make_paper(temp_db)
        bal = paper.get_balances()
        assert "TMN" in bal
        for v in bal.values():
            assert set(v.keys()) == {"free", "locked"}
            assert isinstance(v["free"], float)
            assert isinstance(v["locked"], float)

    def test_get_open_orders_lists_resting(self, temp_db):
        paper = make_paper(temp_db)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 99.0)
        res = paper.get_open_orders("BTCTMN")
        assert len(res["orders"]) == 1
        assert res["orders"][0]["active"] is True

    def test_client_id_unique(self, temp_db):
        paper = make_paper(temp_db)
        r1 = paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        r2 = paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        assert r1["order"]["clientOrderId"] != r2["order"]["clientOrderId"]

    def test_state_persists_across_restart(self, temp_db):
        paper = make_paper(temp_db)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        store_db.close_db()
        store_db.init_db(temp_db)
        paper2 = make_paper(temp_db)
        bal = paper2.get_balances()
        assert "BTC" in bal

    def test_store_write_failure_raises(self, temp_db, monkeypatch):
        paper = make_paper(temp_db)
        def fail_insert(*a, **kw):
            return None
        monkeypatch.setattr(store_db, "insert_order", fail_insert)
        with pytest.raises(PaperExchangeError):
            paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)

    def test_orders_rows_written(self, temp_db):
        paper = make_paper(temp_db)
        paper.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        rows = store_db.get_orders_by_status(
            [PaperStatus.FILLED, PaperStatus.NEW, PaperStatus.PARTIALLY_FILLED], True)
        assert rows
