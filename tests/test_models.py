"""Tests for ai/models.py (STEP 4d PART 5). Fake transport only; NO network."""
import dataclasses
import json
import sqlite3
import threading
import time

import pytest

import config
import store.db as store_db
from ai.models import BoxApiClient, LlmError, ModelVote, parse_vote
from ai.prompt import SYSTEM_PROMPT


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "llm_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


VALID = '{"action": "BUY", "confidence": 0.8, "stop_pct": 0.015, "target_pct": 0.04, "reason": "trend up"}'


class FakeTransport:
    """Scripted transport: each HTTP call consumes one script entry.

    Entries: a content string (-> 200 with that content), ("http", code)
    (-> that status), or "timeout" (raises TimeoutError). The last entry
    repeats when the script runs out. Records payloads and thread ids.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.thread_ids = set()
        self.lock = threading.Lock()
        self.sleep = 0.0

    def __call__(self, payload, timeout):
        with self.lock:
            self.calls.append(payload)
            idx = len(self.calls) - 1
            self.thread_ids.add(threading.get_ident())
        if self.sleep:
            time.sleep(self.sleep)
        entry = self.script[min(idx, len(self.script) - 1)]
        if entry == "timeout":
            raise TimeoutError("simulated timeout")
        if isinstance(entry, tuple) and entry[0] == "http":
            return entry[1], {}
        return 200, {"choices": [{"message": {"content": entry}}],
                     "usage": {"prompt_tokens": 111, "completion_tokens": 22}}


def make_client(transport, db=None, json_mode=None, api_key="TEST-SECRET-BOXAPI-999"):
    cfg = config.CONFIG
    if json_mode is not None or api_key is not None:
        cfg = dataclasses.replace(
            cfg,
            trading=dataclasses.replace(cfg.trading, LLM_JSON_MODE=bool(json_mode)),
            llm=dataclasses.replace(cfg.llm, api_key=api_key),
        )
    return BoxApiClient(cfg, db=db, transport=transport)


INPUT = {"symbol": "BTCTMN", "candles": {"15m": [[1, 2, 3, 4, 5, 6]]}}


def llm_rows(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM llm_calls ORDER BY id")]
    con.close()
    return rows


class TestParseVote:
    def test_plain_json(self):
        v = parse_vote(VALID)
        assert v["action"] == "BUY" and v["confidence"] == 0.8

    def test_markdown_fences_and_prose(self):
        v = parse_vote('Here you go:\n```json\n{"action": "hold", "confidence": 0.1,'
                       ' "stop_pct": 0.01, "target_pct": 0.02, "reason": "x"}\n```\nthanks')
        assert v["action"] == "HOLD"

    def test_lowercase_action_normalized(self):
        v = parse_vote('{"action": "buy", "confidence": 0.7, "stop_pct": 0.01, "target_pct": 0.03}')
        assert v["action"] == "BUY"

    def test_bad_action_rejected(self):
        with pytest.raises(LlmError, match="bad action"):
            parse_vote('{"action": "HODL", "confidence": 0.7, "stop_pct": 0.01, "target_pct": 0.03}')

    def test_confidence_out_of_range_rejected(self):
        with pytest.raises(LlmError, match="confidence"):
            parse_vote('{"action": "BUY", "confidence": 1.5, "stop_pct": 0.01, "target_pct": 0.03}')

    def test_string_confidence_rejected(self):
        with pytest.raises(LlmError, match="confidence"):
            parse_vote('{"action": "BUY", "confidence": "0.7", "stop_pct": 0.01, "target_pct": 0.03}')

    def test_missing_fields_rejected(self):
        with pytest.raises(LlmError, match="stop_pct"):
            parse_vote('{"action": "BUY", "confidence": 0.7, "target_pct": 0.03}')

    def test_no_json_rejected(self):
        with pytest.raises(LlmError):
            parse_vote("I think you should buy.")

    def test_empty_rejected(self):
        with pytest.raises(LlmError):
            parse_vote("")

    def test_reason_truncated(self):
        limit = config.CONFIG.llm.llm_reason_max_chars
        v = parse_vote('{"action": "BUY", "confidence": 0.7, "stop_pct": 0.01,'
                       ' "target_pct": 0.03, "reason": "' + "x" * 500 + '"}')
        assert len(v["reason"]) == limit  # config.llm.llm_reason_max_chars

    def test_reason_limit_follows_config_override(self, monkeypatch):
        tuned = dataclasses.replace(
            config.CONFIG,
            llm=dataclasses.replace(config.CONFIG.llm, llm_reason_max_chars=10))
        monkeypatch.setattr(config, "CONFIG", tuned)
        v = parse_vote('{"action": "BUY", "confidence": 0.7, "stop_pct": 0.01,'
                       ' "target_pct": 0.03, "reason": "' + "y" * 500 + '"}')
        assert len(v["reason"]) == 10

    def test_raw_longer_than_4k_stored_in_full(self, temp_db):
        """v2.2: the old 4k truncation is gone; the full raw text is stored."""
        long_content = ('{"action": "HOLD", "confidence": 0.5, "stop_pct": 0.01,'
                        ' "target_pct": 0.02, "reason": "' + "z" * 9000 + '"}')
        ft = FakeTransport([long_content])
        client = make_client(ft, db=store_db)
        client.decide_all(INPUT, models=["m1"])
        row = llm_rows(temp_db)[0]
        assert len(row["raw_output"]) == len(long_content)  # > 4k, not cut

    def test_raw_capped_at_config_max(self, temp_db):
        cfg = dataclasses.replace(
            config.CONFIG,
            llm=dataclasses.replace(config.CONFIG.llm, llm_raw_store_max_chars=100))
        ft = FakeTransport([VALID + " ".join(["pad"] * 2000)])
        client = BoxApiClient(cfg, db=store_db, transport=ft)
        client.decide_all(INPUT, models=["m1"])
        row = llm_rows(temp_db)[0]
        assert len(row["raw_output"]) == 100


class TestDecideAll:
    def test_valid_votes_from_all_models(self, temp_db):
        ft = FakeTransport([VALID])
        client = make_client(ft, db=store_db)
        votes = client.decide_all(INPUT, models=["m1", "m2", "m3"])
        assert [v.model for v in votes] == ["m1", "m2", "m3"]
        assert all(v.valid is True for v in votes)
        assert all(v.action == "BUY" for v in votes)
        assert votes[0].confidence == 0.8
        assert len(ft.calls) == 3

    def test_every_call_logged(self, temp_db):
        ft = FakeTransport([VALID])
        client = make_client(ft, db=store_db)
        client.decide_all(INPUT, models=["m1", "m2"], decision_id=7, run_id=3)
        rows = llm_rows(temp_db)
        assert len(rows) == 2
        assert rows[0]["model"] in {"m1", "m2"}
        assert rows[0]["decision_id"] == 7 and rows[0]["run_id"] == 3
        assert rows[0]["valid"] == 1
        assert rows[0]["prompt_tokens"] == 111 and rows[0]["completion_tokens"] == 22
        assert rows[0]["latency_ms"] >= 0
        assert "trend up" in rows[0]["raw_output"]
        assert json.loads(rows[0]["parsed_json"])["action"] == "BUY"
        assert rows[0]["error"] is None

    def test_invalid_json_reasked_once_then_valid(self, temp_db):
        ft = FakeTransport(["not json at all", VALID])
        client = make_client(ft, db=store_db)
        votes = client.decide_all(INPUT, models=["m1"])
        assert votes[0].valid is True
        assert votes[0].action == "BUY"
        assert len(ft.calls) == 2          # one re-ask
        rows = llm_rows(temp_db)
        assert len(rows) == 2
        assert rows[0]["valid"] == 0 and "JSON" in rows[0]["error"]
        assert rows[1]["valid"] == 1

    def test_invalid_json_twice_becomes_hold(self, temp_db):
        ft = FakeTransport(["garbage", "still not json"])
        client = make_client(ft, db=store_db)
        votes = client.decide_all(INPUT, models=["m1"])
        v = votes[0]
        assert isinstance(v, ModelVote)
        assert v.valid is False
        assert v.action == "HOLD"
        assert v.error
        assert len(ft.calls) == 2

    def test_timeout_retry_then_success(self, temp_db):
        ft = FakeTransport(["timeout", VALID])
        client = make_client(ft, db=store_db)
        votes = client.decide_all(INPUT, models=["m1"])
        assert votes[0].valid is True
        assert len(ft.calls) == 2
        rows = llm_rows(temp_db)
        assert rows[0]["error"] == "timeout" and rows[0]["valid"] == 0

    def test_timeout_twice_becomes_hold(self, temp_db):
        ft = FakeTransport(["timeout", "timeout"])
        client = make_client(ft, db=store_db)
        votes = client.decide_all(INPUT, models=["m1"])
        assert votes[0].valid is False
        assert votes[0].action == "HOLD"
        assert votes[0].error == "timeout"
        assert len(ft.calls) == 2          # exactly one retry

    def test_5xx_retry_then_success(self):
        ft = FakeTransport([("http", 503), VALID])
        client = make_client(ft)
        votes = client.decide_all(INPUT, models=["m1"])
        assert votes[0].valid is True
        assert len(ft.calls) == 2

    def test_4xx_no_retry(self, temp_db):
        ft = FakeTransport([("http", 401)])
        client = make_client(ft, db=store_db)
        votes = client.decide_all(INPUT, models=["m1"])
        assert votes[0].valid is False
        assert votes[0].error == "http_401"
        assert len(ft.calls) == 1           # client errors are not retried
        assert len(llm_rows(temp_db)) == 1

    def test_parallelism(self, temp_db):
        ft = FakeTransport([VALID])
        ft.sleep = 0.25
        client = make_client(ft, db=store_db)
        t0 = time.perf_counter()
        votes = client.decide_all(INPUT, models=["m1", "m2", "m3"])
        wall = time.perf_counter() - t0
        assert len(votes) == 3 and all(v.valid for v in votes)
        # sequential would be >= 0.75s; parallel should be well under
        assert wall < 0.7
        assert len(ft.thread_ids) > 1       # really ran on several threads

    def test_no_logging_when_db_none(self):
        ft = FakeTransport([VALID])
        client = make_client(ft, db=None)
        votes = client.decide_all(INPUT, models=["m1", "m2"])
        assert all(v.valid for v in votes)  # no crash, nothing to log

    def test_empty_models_list(self):
        client = make_client(FakeTransport([VALID]))
        assert client.decide_all(INPUT, models=[]) == []

    def test_models_default_from_config(self):
        ft = FakeTransport([VALID])
        client = make_client(ft)
        votes = client.decide_all(INPUT)
        assert [v.model for v in votes] == list(config.CONFIG.llm.MODELS)


class TestJsonModeAndSecrets:
    def test_response_format_only_in_json_mode(self):
        ft = FakeTransport([VALID])
        client = make_client(ft, json_mode=False)
        client.decide_all(INPUT, models=["m1"])
        assert "response_format" not in ft.calls[0]

        ft2 = FakeTransport([VALID])
        client2 = make_client(ft2, json_mode=True)
        client2.decide_all(INPUT, models=["m1"])
        assert ft2.calls[0]["response_format"] == {"type": "json_object"}

    def test_payload_shape(self):
        ft = FakeTransport([VALID])
        client = make_client(ft)
        client.decide_all(INPUT, models=["m1"])
        payload = ft.calls[0]
        assert payload["model"] == "m1"
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][0]["content"] == SYSTEM_PROMPT
        assert json.loads(payload["messages"][1]["content"])["symbol"] == "BTCTMN"

    def test_api_key_never_in_llm_calls(self, temp_db):
        secret = "TEST-SECRET-BOXAPI-999"
        ft = FakeTransport([VALID, "junk", "junk"])
        client = make_client(ft, db=store_db)  # cfg carries the secret
        client.decide_all(INPUT, models=["m1", "m2"])
        for row in llm_rows(temp_db):
            blob = json.dumps(row)
            assert secret not in blob

    def test_default_transport_sends_bearer_header(self, monkeypatch):
        secret = "TEST-SECRET-BOXAPI-999"
        cfg = dataclasses.replace(
            config.CONFIG,
            llm=dataclasses.replace(config.CONFIG.llm, api_key=secret))
        client = BoxApiClient(cfg)  # real default transport
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers

            class R:
                status_code = 200

                def json(self):
                    return {"choices": [{"message": {"content": VALID}}]}
            return R()

        monkeypatch.setattr("ai.models.requests.post", fake_post)
        status, body = client._default_transport({"model": "m"}, 5)
        assert status == 200
        assert captured["url"].endswith("/chat/completions")
        assert captured["headers"]["Authorization"] == f"Bearer {secret}"
        assert _extract_ok(body)

    def test_default_transport_tolerates_bad_json(self, monkeypatch):
        client = BoxApiClient(config.CONFIG)

        class R:
            status_code = 200
            text = "not json"

            def json(self):
                raise ValueError("bad")

        monkeypatch.setattr("ai.models.requests.post", lambda *a, **kw: R())
        status, body = client._default_transport({"model": "m"}, 5)
        assert status == 200
        assert body == {"_raw_text": "not json"}


def _extract_ok(body):
    from ai.models import _extract_content
    content, err = _extract_content(body)
    return err is None and content == VALID


class TestAsk:
    def test_ask_ok(self):
        ft = FakeTransport([VALID])
        client = make_client(ft)
        res = client.ask("m1", "system", "user")
        assert res["ok"] is True
        assert res["parsed"]["action"] == "BUY"
        assert res["error"] is None
        assert res["attempts"] == 1

    def test_ask_timeout_then_ok(self):
        ft = FakeTransport(["timeout", VALID])
        client = make_client(ft)
        res = client.ask("m1", "system", "user")
        assert res["ok"] is True and res["attempts"] == 2

    def test_ask_invalid_json_reported(self):
        ft = FakeTransport(["nope"])
        client = make_client(ft)
        res = client.ask("m1", "system", "user")
        assert res["ok"] is False
        assert res["error"]
        assert res["content"] == "nope"

    def test_ask_json_mode_override(self):
        ft = FakeTransport([VALID])
        client = make_client(ft, json_mode=False)
        client.ask("m1", "s", "u", json_mode=True)
        assert ft.calls[0]["response_format"] == {"type": "json_object"}
