"""Tests for WallexClient write methods (PART 2)."""
import pytest
from unittest.mock import patch, MagicMock
import dataclasses
import config
from exchange.wallex import WallexClient, WallexError
import store.db as store_db


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "wallex_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


def _cfg_dry(dry: bool):
    return dataclasses.replace(config.CONFIG, trading=dataclasses.replace(config.CONFIG.trading, dry_run=dry))


def make_mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {}
    resp.raise_for_status.return_value = None
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


class TestWallexOrders:
    def test_create_order_body_casing_and_omit_none(self, temp_db):
        payload = {"result": {"clientOrderId": "abc123", "status": "NEW"}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.create_order(symbol="BTCTMN", side="buy", type="limit",
                                      quantity=1.5, price=100.0, stop_price=None, client_id="myid")
            assert res["order"]["clientOrderId"] == "abc123"
            called_json = mock_req.call_args[1]["json"]
            assert called_json["side"] == "BUY"
            assert called_json["type"] == "LIMIT"
            assert called_json["symbol"] == "BTCTMN"
            assert called_json["price"] in ("100.0", "100")
            assert called_json["quantity"] == "1.5"
            assert "stop_Price" not in called_json
            assert called_json["client_id"] == "myid"

    def test_cancel_order_path(self, temp_db):
        payload = {"result": {"clientOrderId": "abc123", "status": "CANCELED"}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.cancel_order("abc123")
            assert res["order"]["status"] == "CANCELED"
            called_url = mock_req.call_args[0][1]
            assert "/v1/account/orders/abc123" in called_url
            assert mock_req.call_args[0][0] == "DELETE"

    def test_get_order_path(self, temp_db):
        payload = {"result": {"clientOrderId": "abc123", "status": "FILLED"}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.get_order("abc123")
            assert res["order"]["status"] == "FILLED"
            called_url = mock_req.call_args[0][1]
            assert "/v1/account/orders/abc123" in called_url
            assert mock_req.call_args[0][0] == "GET"

    def test_get_open_orders(self, temp_db):
        payload = {"result": {"key1": [{"clientOrderId": "o1"}], "key2": []}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.get_open_orders(symbol="BTCTMN")
            assert len(res["orders"]) == 1
            assert res["orders"][0]["clientOrderId"] == "o1"
            called_params = mock_req.call_args[1]["params"]
            assert called_params["symbol"] == "BTCTMN"

    def test_safety_interlock_dry_run_blocks_writes(self, temp_db):
        client = WallexClient(_cfg_dry(True), db=temp_db)
        with pytest.raises(WallexError) as exc:
            client.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
        assert "DRY_RUN" in str(exc.value)
        with pytest.raises(WallexError):
            client.cancel_order("id")
        with pytest.raises(WallexError):
            client.get_order("id")
        with pytest.raises(WallexError):
            client.get_open_orders()

    def test_api_key_never_in_error(self, temp_db):
        client = WallexClient(_cfg_dry(False), db=temp_db)
        with patch("requests.Session.request", side_effect=Exception("boom")):
            try:
                client.create_order("BTCTMN", "BUY", "LIMIT", 1.0, 100.0)
            except Exception as e:
                assert config.CONFIG.exchange.api_key not in str(e)

    # ---- new tests per follow-up ----

    def test_create_order_201_success(self, temp_db):
        payload = {"result": {"clientOrderId": "abc123", "status": "NEW"}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(status_code=201, json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.create_order(symbol="BTCTMN", side="buy", type="limit",
                                      quantity=1.5, price=100.0)
            assert res["order"]["clientOrderId"] == "abc123"
            # ensure 201 treated as success (no exception)

    def test_create_order_price_qty_strings_no_float_artifacts(self, temp_db):
        payload = {"result": {"clientOrderId": "abc123", "status": "NEW"}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="BTCTMN", side="buy", type="limit",
                                quantity=0.3, price=100.0)
            called_json = mock_req.call_args[1]["json"]
            # price and quantity must be strings, no scientific notation or long float artifacts
            assert isinstance(called_json["price"], str)
            assert isinstance(called_json["quantity"], str)
            assert "e" not in called_json["price"].lower()
            assert "e" not in called_json["quantity"].lower()

    def test_client_id_validation(self, temp_db):
        from exchange.wallex import _validate_client_id
        # valid
        _validate_client_id("ag_123_BTCTMN_1")  # should not raise
        # invalid characters
        with pytest.raises(ValueError):
            _validate_client_id("ag-123")
        with pytest.raises(ValueError):
            _validate_client_id("ag 123")
        with pytest.raises(ValueError):
            _validate_client_id("ag.123")

    def test_stop_price_only_for_stop_types(self, temp_db):
        payload = {"result": {"clientOrderId": "abc123", "status": "NEW"}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            # LIMIT should not include stop_Price
            client.create_order(symbol="BTCTMN", side="buy", type="limit",
                                quantity=1.0, price=100.0, stop_price=99.0)
            called_json = mock_req.call_args[1]["json"]
            assert "stop_Price" not in called_json
            # STOP_LIMIT should include
            client.create_order(symbol="BTCTMN", side="buy", type="stop_limit",
                                quantity=1.0, price=100.0, stop_price=99.0)
            called_json2 = mock_req.call_args[1]["json"]
            assert "stop_Price" in called_json2

    def test_open_orders_parser_dict_of_lists(self, temp_db):
        payload = {"result": {"k1": [{"clientOrderId": "o1"}, {"clientOrderId": "o2"}],
                               "k2": [{"clientOrderId": "o3"}]}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)):
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.get_open_orders()
            assert len(res["orders"]) == 3
            ids = {o["clientOrderId"] for o in res["orders"]}
            assert ids == {"o1", "o2", "o3"}

    def test_open_orders_parser_plain_list(self, temp_db):
        payload = {"result": [{"clientOrderId": "o1"}, {"clientOrderId": "o2"}], "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)):
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.get_open_orders()
            assert len(res["orders"]) == 2

    def test_open_orders_parser_empty_dict(self, temp_db):
        payload = {"result": {}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)):
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.get_open_orders()
            assert res["orders"] == []

    def test_cancel_order_delete_no_body(self, temp_db):
        payload = {"result": {"clientOrderId": "abc123", "status": "CANCELED"}, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.cancel_order("abc123")
            # ensure DELETE method and no json body
            assert mock_req.call_args[0][0] == "DELETE"
            assert mock_req.call_args[1].get("json") is None

    def test_parser_tolerates_missing_fee_and_executed_percent(self, temp_db):
        # create a client with dry_run False to use parser via create_order response
        payload = {"result": {
            "clientOrderId": "abc123",
            "status": "NEW",
            "executedPercent": "50",      # string
            "fee": None,
            "fills": []
        }, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)):
            client = WallexClient(_cfg_dry(False), db=temp_db)
            res = client.create_order(symbol="BTCTMN", side="buy", type="limit",
                                      quantity=1.0, price=100.0)
            order = res["order"]
            assert order["executedPercent"] == 50.0
            assert order["fee"] is None
            # also test numeric executedPercent
        payload2 = {"result": {
            "clientOrderId": "abc124",
            "status": "NEW",
            "executedPercent": 75,      # number
            "fee": "0.001"
        }, "success": True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload2)):
            res2 = client.create_order(symbol="BTCTMN", side="buy", type="limit",
                                       quantity=1.0, price=100.0)
            assert res2["order"]["executedPercent"] == 75.0
            assert res2["order"]["fee"] == 0.001


def _routed_mock(markets_payload, order_payload):
    """A requests.Session.request side effect that routes by URL:
    /markets -> markets_payload, anything else -> order_payload."""
    def fake_request(method, url, **kwargs):
        if "/markets" in url:
            return make_mock_response(json_data=markets_payload)
        return make_mock_response(json_data=order_payload)
    return fake_request


def _markets_payload(symbol, price_precision, amount_precision):
    return {
        "result": {"markets": [
            {"symbol": symbol, "base_asset": "X", "quote_asset": "TMN",
             "price_precision": price_precision, "amount_precision": amount_precision},
        ]},
        "message": "ok", "success": True,
    }


_ORDER_PAYLOAD = {"result": {"clientOrderId": "abc123", "status": "NEW"}, "success": True}


class TestCreateOrderPrecisionViaMarketInfo:
    """STEP 4d PART 0.1: create_order ALWAYS formats price/quantity with the
    market's precisions fetched via get_market_info (Decimal, ROUND_DOWN)."""

    def test_real_422_case_price_precision_0(self, temp_db):
        # best-known real failure: BTCTMN price 17671321516.8 -> 422 "price must have 0 decimals"
        markets = _markets_payload("BTCTMN", 0, 8)
        with patch("requests.Session.request",
                   side_effect=_routed_mock(markets, _ORDER_PAYLOAD)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="BTCTMN", side="BUY", type="LIMIT",
                                quantity=0.00000707, price=17671321516.8)
            called_json = mock_req.call_args[1]["json"]
            assert called_json["price"] == "17671321516"
            assert "." not in called_json["price"]
            assert called_json["quantity"] == "0.00000707"

    def test_quantity_precision_8(self, temp_db):
        markets = _markets_payload("BTCTMN", 0, 8)
        with patch("requests.Session.request",
                   side_effect=_routed_mock(markets, _ORDER_PAYLOAD)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="BTCTMN", side="BUY", type="LIMIT",
                                quantity=0.123456789, price=100.0)
            called_json = mock_req.call_args[1]["json"]
            assert called_json["quantity"] == "0.12345678"

    def test_quantity_precision_5(self, temp_db):
        markets = _markets_payload("ETHTMN", 0, 5)
        with patch("requests.Session.request",
                   side_effect=_routed_mock(markets, _ORDER_PAYLOAD)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="ETHTMN", side="BUY", type="LIMIT",
                                quantity=0.123456789, price=100.0)
            called_json = mock_req.call_args[1]["json"]
            assert called_json["quantity"] == "0.12345"

    def test_quantity_precision_1(self, temp_db):
        markets = _markets_payload("XRPTMN", 0, 1)
        with patch("requests.Session.request",
                   side_effect=_routed_mock(markets, _ORDER_PAYLOAD)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="XRPTMN", side="BUY", type="LIMIT",
                                quantity=12.345, price=30000.7)
            called_json = mock_req.call_args[1]["json"]
            assert called_json["quantity"] == "12.3"
            assert called_json["price"] == "30000"

    def test_market_order_quantity_also_formatted(self, temp_db):
        markets = _markets_payload("XRPTMN", 0, 1)
        with patch("requests.Session.request",
                   side_effect=_routed_mock(markets, _ORDER_PAYLOAD)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="XRPTMN", side="SELL", type="MARKET",
                                quantity=12.345)
            called_json = mock_req.call_args[1]["json"]
            assert called_json["quantity"] == "12.3"
            assert "price" not in called_json

    def test_stop_price_formatted_with_precision(self, temp_db):
        markets = _markets_payload("BTCTMN", 0, 8)
        with patch("requests.Session.request",
                   side_effect=_routed_mock(markets, _ORDER_PAYLOAD)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="BTCTMN", side="SELL", type="STOP_LIMIT",
                                quantity=1.0, price=100.5, stop_price=99.7)
            called_json = mock_req.call_args[1]["json"]
            assert called_json["stop_Price"] == "99"

    def test_markets_fetch_failure_falls_back_tolerant(self, temp_db):
        # markets endpoint broken -> no crash, tolerant formatting (no precision)
        broken = {"result": {"unexpected": True}, "success": True}
        with patch("requests.Session.request",
                   side_effect=_routed_mock(broken, _ORDER_PAYLOAD)) as mock_req:
            client = WallexClient(_cfg_dry(False), db=temp_db)
            client.create_order(symbol="BTCTMN", side="BUY", type="LIMIT",
                                quantity=1.5, price=100.0)
            called_json = mock_req.call_args[1]["json"]
            assert isinstance(called_json["price"], str)
            assert "e" not in called_json["price"].lower()
