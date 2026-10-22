"""Tests for exchange/wallex.py (PART 1). All HTTP mocked."""
import json
import pytest
from unittest.mock import patch, MagicMock
import requests

import config
from exchange.wallex import WallexClient, WallexError
import store.db as store_db


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


def make_mock_response(status_code=200, json_data=None, text="", headers=None, raise_exc=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = headers or {}
    if raise_exc:
        resp.raise_for_status.side_effect = raise_exc
    else:
        resp.raise_for_status.return_value = None
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = json.JSONDecodeError("no json", "", 0)
    return resp


class TestWallexClient:
    def test_get_markets_sends_api_key_and_parses(self, temp_db):
        markets_payload = {
            "result": {"markets": [
                {"symbol": "BTCTMN", "price_precision": 0, "amount_precision": 8, "flags": [], "is_otc": True,
                 "is_market_type_enable": True, "is_zero_fee": False, "quote_volume_24h": 123.4,
                 "otc_min_notional": 90000, "otc_max_notional": 400000000},
                {"symbol": "ETHTMN", "price_precision": 2, "amount_precision": 6, "flags": [], "is_otc": False,
                 "is_market_type_enable": True, "is_zero_fee": False, "quote_volume_24h": 567.8,
                 "otc_min_notional": 90000, "otc_max_notional": 200000000},
            ]},
            "message": "ok",
            "success": True
        }
        with patch("requests.Session.request", return_value=make_mock_response(json_data=markets_payload)) as mock_req:
            client = WallexClient(config.CONFIG, db=temp_db)
            markets = client.get_markets()
            assert len(markets) == 2
            assert markets[0]["symbol"] == "BTCTMN"
            # verify header present in session
            assert client.session.headers["x-api-key"] == config.CONFIG.exchange.api_key

    def test_get_depth_parsing_string_and_number_sum(self, temp_db):
        # sum as string
        depth_payload_str = {
            "result": {
                "bid": [{"price": "50000", "quantity": "0.1", "sum": "5000"}],
                "ask": [{"price": "50010", "quantity": "0.2", "sum": "10002"}],
            },
            "message": "ok", "success": True
        }
        with patch("requests.Session.request", return_value=make_mock_response(json_data=depth_payload_str)):
            client = WallexClient(config.CONFIG, db=temp_db)
            depth = client.get_depth("BTCTMN")
            assert depth["bids"][0] == (50000.0, 0.1, 5000.0)
            assert depth["asks"][0] == (50010.0, 0.2, 10002.0)

        # sum as number
        depth_payload_num = {
            "result": {
                "bid": [{"price": 50000, "quantity": 0.1, "sum": 5000}],
                "ask": [{"price": 50010, "quantity": 0.2, "sum": 10002}],
            },
            "message": "ok", "success": True
        }
        with patch("requests.Session.request", return_value=make_mock_response(json_data=depth_payload_num)):
            client = WallexClient(config.CONFIG, db=temp_db)
            depth = client.get_depth("BTCTMN")
            assert depth["bids"][0] == (50000.0, 0.1, 5000.0)

    def test_get_depth_ordering(self, temp_db):
        payload = {
            "result": {
                "bid": [
                    {"price": "50000", "quantity": "1", "sum": "50000"},
                    {"price": "50100", "quantity": "1", "sum": "50100"},
                ],
                "ask": [
                    {"price": "50200", "quantity": "1", "sum": "50200"},
                    {"price": "50150", "quantity": "1", "sum": "50150"},
                ],
            },
            "message": "ok", "success": True
        }
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            depth = client.get_depth("BTCTMN")
            # bids highest first
            assert [p for p,_,_ in depth["bids"]] == [50100.0, 50000.0]
            # asks lowest first
            assert [p for p,_,_ in depth["asks"]] == [50150.0, 50200.0]

    def test_get_candles_ok_no_data_error(self, temp_db):
        # ok
        ok_payload = {"s":"ok","t":[1,2],"o":["1","2"],"h":["2","3"],"l":["0.5","1"],"c":["1.5","2.5"],"v":["10","20"]}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=ok_payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            candles = client.get_candles("BTCTMN","60",1,2)
            assert len(candles)==2
            assert candles[0]["t"]==1 and candles[0]["o"]==1.0

        # no_data
        nodata_payload = {"s":"no_data","t":[],"o":[],"h":[],"l":[],"c":[],"v":[]}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=nodata_payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            candles = client.get_candles("BTCTMN","60",1,2)
            assert candles == []

        # error
        err_payload = {"s":"error","errmsg":"something"}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=err_payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            with pytest.raises(WallexError) as exc:
                client.get_candles("BTCTMN","60",1,2)
            assert exc.value.retryable is False

    def test_http_errors_retryable_flags(self, temp_db):
        client = WallexClient(config.CONFIG, db=temp_db)
        # 429 retryable
        with patch("requests.Session.request", return_value=make_mock_response(429, text="rate limit")):
            with pytest.raises(WallexError) as exc:
                client.get_markets()
            assert exc.value.retryable is True
        # 500 retryable
        with patch("requests.Session.request", return_value=make_mock_response(500, text="server error")):
            with pytest.raises(WallexError) as exc:
                client.get_markets()
            assert exc.value.retryable is True
        # 401 not retryable
        with patch("requests.Session.request", return_value=make_mock_response(401, text="unauthorized")):
            with pytest.raises(WallexError) as exc:
                client.get_markets()
            assert exc.value.retryable is False
        # 400 not retryable
        with patch("requests.Session.request", return_value=make_mock_response(400, text="bad request")):
            with pytest.raises(WallexError) as exc:
                client.get_markets()
            assert exc.value.retryable is False

    def test_network_errors_retryable(self, temp_db):
        client = WallexClient(config.CONFIG, db=temp_db)
        with patch("requests.Session.request", side_effect=requests.exceptions.Timeout("timeout")):
            with pytest.raises(WallexError) as exc:
                client.get_markets()
            assert exc.value.retryable is True
        with patch("requests.Session.request", side_effect=requests.exceptions.ConnectionError("conn")):
            with pytest.raises(WallexError) as exc:
                client.get_markets()
            assert exc.value.retryable is True

    def test_api_key_never_in_db_or_exception(self, temp_db):
        payload = {"result":{"markets":[]},"message":"ok","success":True}
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            client.get_markets()
        # check api_calls table does not contain the key
        import sqlite3
        con = sqlite3.connect(temp_db)
        cur = con.execute("SELECT request_json, response_text, error FROM api_calls")
        rows = cur.fetchall()
        con.close()
        for req, resp, err in rows:
            assert config.CONFIG.exchange.api_key not in (req or "")
            assert config.CONFIG.exchange.api_key not in (resp or "")
            assert config.CONFIG.exchange.api_key not in (err or "")

    def test_db_logging_failure_does_not_break_request(self, temp_db):
        payload = {"result":{"markets":[]},"message":"ok","success":True}
        # make db logging raise by closing connection
        store_db.close_db()
        with patch("requests.Session.request", return_value=make_mock_response(json_data=payload)):
            client = WallexClient(config.CONFIG, db=temp_db)  # db path now invalid
            # should not raise
            markets = client.get_markets()
            assert markets == []

    def test_get_balances_and_fee_parsing(self, temp_db):
        bal_payload = {
            "result": {
                "balances": {
                    "BTC": {"value": "1.5", "locked": "0.1"},
                    "USDT": {"value": "1000", "locked": "0"},
                }
            },
            "message": "ok", "success": True
        }
        with patch("requests.Session.request", return_value=make_mock_response(json_data=bal_payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            bal = client.get_balances()
            assert bal["BTC"]["free"] == 1.5
            assert bal["BTC"]["locked"] == 0.1

        fee_payload = {
            "result": {
                "BTCTMN": {"makerFeeRate": "0.002", "takerFeeRate": "0.003", "recent_days_sum": 1000},
                "ETHTMN": {"makerFeeRate": "0.0025", "takerFeeRate": "0.003", "recent_days_sum": 0},
            },
            "message": "ok", "success": True
        }
        with patch("requests.Session.request", return_value=make_mock_response(json_data=fee_payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            fees = client.get_fee()
            assert set(fees.keys()) == {"BTCTMN", "ETHTMN"}
            assert fees["BTCTMN"]["maker_rate"] == 0.002
            assert fees["ETHTMN"]["taker_rate"] == 0.003
            # get_fee_for uses the cache
            assert client.get_fee_for("BTCTMN") == {"maker_rate": 0.002, "taker_rate": 0.003}
            assert client.get_fee_for("MISSING") is None

    def test_get_market_info(self, temp_db):
        markets_payload = {
            "result": {"markets": [
                {"symbol": "BTCTMN", "base_asset": "BTC", "quote_asset": "TMN",
                 "price_precision": 0, "amount_precision": 8,
                 "is_market_type_enable": True, "is_zero_fee": False,
                 "flags": [], "quote_volume_24h": 123.4},
                {"symbol": "SOLTMN", "price_precision": 0, "amount_precision": 5,
                 "is_market_type_enable": False, "is_zero_fee": False,
                 "flags": ["isRisky"], "quote_volume_24h": 456.7},
            ]},
            "message": "ok", "success": True
        }
        with patch("requests.Session.request", return_value=make_mock_response(json_data=markets_payload)):
            client = WallexClient(config.CONFIG, db=temp_db)
            mi = client.get_market_info("BTCTMN")
            assert mi is not None
            assert mi.base_asset == "BTC"
            assert mi.quote_asset == "TMN"
            assert mi.price_precision == 0
            assert mi.amount_precision == 8
            assert mi.is_market_type_enable is True
            # base_asset fallback strips the suffix
            mi2 = client.get_market_info("SOLTMN")
            assert mi2.base_asset == "SOL"
            assert mi2.flags == ["isRisky"]
            assert client.get_market_info("NOPE") is None

    def test_base_asset_helper(self):
        from exchange.wallex import base_asset
        assert base_asset("BTCTMN") == "BTC"
        assert base_asset("BTCUSDT") == "BTC"
        assert base_asset("USDTTMN") == "USDT"
        assert base_asset("XYZ") == "XYZ"