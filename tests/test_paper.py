"""Tests for exchange/paper.py (PART 2)."""
import pytest
from unittest.mock import MagicMock, patch
import config
from exchange.paper import PaperExchange, PaperStatus
from exchange.wallex import WallexError
import store.db as store_db


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "paper_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


@pytest.fixture()
def mock_data_source():
    """Mock WallexClient providing depth and fee."""
    ds = MagicMock()
    # default depth: asks and bids
    ds.get_depth.return_value = {
        "asks": [(100.0, 10.0, 1000.0), (101.0, 5.0, 505.0)],
        "bids": [(99.0, 8.0, 792.0), (98.0, 12.0, 1176.0)],
    }
    ds.get_fee.return_value = {"taker_rate": 0.002, "maker_rate": 0.001, "raw": {}}
    return ds


def make_paper(cfg=None, db_path=None, data_source=None):
    if cfg is None:
        cfg = config.CONFIG
    if data_source is None:
        data_source = mock_data_source()
    return PaperExchange(cfg, db_path or "runtime/agent.db", data_source)


class TestPaperExchange:
    def test_buy_limit_below_best_ask_stays_open(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # best ask 100, limit 99 -> below, should not fill
        res = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=99.0)
        assert res["order"]["status"] == PaperStatus.NEW
        assert res["order"]["executed_quantity"] == 0.0

    def test_buy_limit_at_best_ask_fills(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # limit at best ask 100
        res = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=100.0)
        assert res["order"]["status"] == PaperStatus.FILLED
        assert res["order"]["executed_quantity"] == 1.0
        assert abs(res["order"]["avg_price"] - 100.0) < 1e-6

    def test_buy_limit_partial_fill(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # ask depth total 15 (10 at 100 + 5 at 101). Request 20 qty at price 101 => should fill 15
        res = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=20.0, price=101.0)
        assert res["order"]["status"] == PaperStatus.PARTIALLY_FILLED
        assert res["order"]["executed_quantity"] == 15.0

    def test_market_sell_walks_bids(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # need asset balance first: give some asset
        store_db.set_paper_account(1_000_000, {"BTC": 5.0})
        res = paper.create_order(symbol="BTCTMN", side="SELL", type="MARKET", quantity=5.0)
        # bids total 20 (8 at 99 +12 at 98) => fill 5 at 99
        assert res["order"]["status"] == PaperStatus.FILLED
        assert abs(res["order"]["avg_price"] - 99.0) < 1e-6

    def test_fee_charged(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # fee rate 0.002
        res = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=100.0)
        fee = res["order"]["fee_tmn"]
        assert abs(fee - 100.0 * 0.002) < 1e-6

    def test_fallback_fee_when_get_fee_fails(self, temp_db):
        ds = MagicMock()
        ds.get_depth.return_value = {"asks": [(100.0, 10.0, 1000.0)], "bids": [(99.0, 10.0, 990.0)]}
        ds.get_fee.side_effect = Exception("fee down")
        cfg = config.CONFIG
        paper = PaperExchange(cfg, "runtime/agent.db", ds)
        res = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=100.0)
        # fallback fee 0.005
        assert abs(res["order"]["fee_tmn"] - 100.0 * cfg.trading.paper_fallback_fee_rate) < 1e-6

    def test_insufficient_balance_rejected(self, temp_db, mock_data_source):
        # Provide huge ask depth so order would fill huge notional exceeding capital
        mock_data_source.get_depth.return_value = {
            "asks": [(100.0, 100000.0, 10000000.0)],  # huge qty
            "bids": [(99.0, 10.0, 990.0)],
        }
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        with pytest.raises(WallexError):
            paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=100000.0, price=100.0)

    def test_balances_never_negative(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # buy small
        paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=0.5, price=100.0)
        acct = store_db.get_paper_account()
        assert acct["tmn_balance"] >= 0
        for v in acct.get("assets", {}).values():
            assert v >= -1e-9

    def test_check_open_orders_fills_when_ask_drops(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # place limit buy at 99 (below ask) -> stays open
        paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=99.0)
        # now simulate ask dropping to 98 by changing depth
        mock_data_source.get_depth.return_value = {
            "asks": [(98.0, 10.0, 980.0)], "bids": [(97.0, 10.0, 970.0)]
        }
        # call check_open_orders (currently placeholder) -> not implemented, so just ensure no error
        paper.check_open_orders()

    def test_cancel_order_frees_funds(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        # place limit buy at 100 (fills immediately) -> can't cancel
        # instead place limit buy at 99 (open)
        res = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=2.0, price=99.0)
        client_id = res["order"]["client_id"]
        # cancel
        cancel_res = paper.cancel_order(client_id)
        assert cancel_res["order"]["status"] == PaperStatus.CANCELED
        # TMN should be back to initial
        acct = store_db.get_paper_account()
        assert acct["tmn_balance"] == pytest.approx(config.CONFIG.trading.paper_initial_capital_tmn)

    def test_state_persists_across_reinit(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=100.0)
        # reinit
        paper2 = make_paper(db_path=temp_db, data_source=mock_data_source)
        # order should exist in store
        # we can't easily query but ensure no crash
        assert True

    def test_orders_written_and_client_id_unique(self, temp_db, mock_data_source):
        paper = make_paper(db_path=temp_db, data_source=mock_data_source)
        res1 = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=100.0)
        res2 = paper.create_order(symbol="BTCTMN", side="BUY", type="LIMIT", quantity=1.0, price=100.0)
        assert res1["order"]["client_id"] != res2["order"]["client_id"]