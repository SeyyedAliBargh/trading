"""Tests for ui/server.py + the dashboard front-end (read-only, no network)."""
import hashlib
import json
import sqlite3
import urllib.error
import urllib.request

import pytest

import store.db as store_db
from ui.server import _connect, start_ui_thread

SECRET = "TEST-SECRET-NEVER-LEAK-123"


@pytest.fixture()
def seeded_db(tmp_path):
    path = tmp_path / "ui_test.db"
    store_db.init_db(str(path))
    run_id = store_db.start_run("paper",
                                 {"llm": {"MODELS": ["m1", "m2"]},
                                  "trading": {"SYMBOLS": ["BTCTMN", "ETHTMN"]}},
                                 git_commit="test")
    d1 = store_db.insert_decision(
        run_id, symbol="BTCTMN", prompt_version="v2.1", source="SCHEDULE",
        decision_uid="a" * 32, final_action="BUY", confidence=0.7,
        stop_pct=0.015, target_pct=0.045, guard_allowed=1, outcome="entered",
        votes_json=[
            {"model": "m1", "action": "BUY", "confidence": 0.7,
             "stop_pct": 0.015, "target_pct": 0.045, "valid": True,
             "reason": "uptrend with rising volume and supportive book"},
            {"model": "m2", "action": "BUY", "confidence": 0.66,
             "stop_pct": 0.016, "target_pct": 0.05, "valid": True,
             "reason": "breakout over the range high"}])
    pid = store_db.insert_position(
        run_id=run_id, symbol="BTCTMN", entry_price=100_000.0, quantity=5.0,
        entry_value_tmn=500_000.0, stop_pct=0.015, target_pct=0.045,
        stop_price=98_500.0, target_price=104_500.0, dry_run=True,
        entry_decision_id=d1)
    assert store_db.close_position(pid, 103_000.0, "TAKE_PROFIT", 14_000.0, 200.0)
    assert store_db.insert_position(
        run_id=run_id, symbol="ETHTMN", entry_price=50_000.0, quantity=2.0,
        entry_value_tmn=100_000.0, stop_pct=0.015, target_pct=0.045,
        stop_price=49_250.0, target_price=52_250.0, dry_run=True,
        entry_decision_id=d1)
    d2 = store_db.insert_decision(
        run_id, symbol="ETHTMN", prompt_version="v2.1", source="TRIGGER",
        decision_uid="b" * 32, final_action="HOLD", confidence=0.7,
        guard_allowed=0, outcome="guard_rejected", reason="net edge too small",
        votes_json=[{"model": "m1", "action": "BUY", "confidence": 0.7,
                     "valid": True, "reason": "r"}])
    store_db.insert_risk_check(run_id, decision_id=d2, rule="net_edge",
                               passed=False, value=-0.01, threshold=0.002,
                               message="net edge -0.01 < min 0.002")
    store_db.insert_risk_check(run_id, decision_id=d2, rule="thin_book",
                               passed=False, value=0, threshold=1,
                               message="thin book")
    store_db.insert_risk_check(run_id, decision_id=d1, rule="order_size",
                               passed=True, value=500_000.0,
                               threshold=100_000.0, message="")
    long_raw = "raw model text " + "z" * 9000
    store_db.insert_llm_call(run_id, decision_id=d1, model="m1",
                             raw_output=long_raw,
                             parsed_json={"action": "BUY"}, valid=True,
                             prompt_tokens=900, completion_tokens=80,
                             latency_ms=1234.0)
    store_db.insert_llm_call(run_id, decision_id=d2, model="m2",
                             raw_output="garbage", valid=False,
                             error="no JSON object in content",
                             prompt_tokens=900, completion_tokens=0,
                             latency_ms=50.0)
    store_db.log_event("INFO", "HEARTBEAT", "guardian alive", None, run_id)
    store_db.log_event("INFO", "GUARDIAN_WATCH",
                       "[GUARDIAN] BTCTMN bid=99000 position",
                       {"symbol": "BTCTMN", "best_bid": 99_000.0,
                        "mid": 99_100.0, "has_position": True,
                        "trigger_move_pct": 0.015, "trigger_window_sec": 300,
                        "trigger_fired": False,
                        "observed_move_pct_signed": 0.4,
                        "move_fraction_of_threshold": 0.27,
                        "cooldown_remaining_sec": 0.0,
                        "entry": 100_000.0, "sl_price": 98_500.0,
                        "tp_price": 104_500.0, "distance_to_sl_pct": 0.5,
                        "distance_to_tp_pct": 5.5}, run_id)
    store_db.log_event("INFO", "TRIGGER", "price moved 2.97%",
                       {"symbol": "ETHTMN"}, run_id)
    store_db.log_event("ERROR", "CRITICAL", "10 close attempts", None, run_id)
    for i in range(600):
        store_db.insert_balance_snapshot(run_id, "paper", 4_000_000.0,
                                         {"TMN": {"free": 4_000_000.0}},
                                         5_000_000.0 + i * 100.0)
    store_db.insert_order(run_id, decision_id=d1, client_id="ag_ui_1",
                          symbol="BTCTMN", side="BUY", type="LIMIT",
                          price=100_000.0, quantity=5.0, status="FILLED",
                          filled_quantity=5.0, avg_fill_price=100_000.0,
                          dry_run=True)
    store_db.close_db()
    yield str(path)


@pytest.fixture()
def empty_db(tmp_path):
    path = tmp_path / "empty.db"
    store_db.init_db(str(path))
    store_db.close_db()
    yield str(path)


@pytest.fixture()
def ui(tmp_path):
    handles = []

    def _start(db_path, port=0, log_path=None):
        handle = start_ui_thread(db_path, "127.0.0.1", port, log_path=log_path)
        assert handle is not None, "expected the UI to start on a free port"
        handles.append(handle)
        return handle

    yield _start
    for handle in handles:
        handle.stop()


def get(handle, path):
    with urllib.request.urlopen(handle.url + path, timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)


def raw_get(handle, path):
    with urllib.request.urlopen(handle.url + path, timeout=5) as r:
        return r.status, r.read(), dict(r.headers)


class TestEndpoints:
    def test_overview_fields(self, seeded_db, ui):
        status, body, headers = get(ui(seeded_db), "/api/overview")
        assert status == 200
        assert body["mode"] == "PAPER"
        assert body["models"] == ["m1", "m2"]
        assert body["prompt_version"] == "v2.1"
        assert body["equity_last"] is not None
        assert body["return_pct"] == pytest.approx(59900.0 / 5000000.0 * 100,
                                                   rel=1e-3)
        assert body["max_drawdown_pct"] == pytest.approx(0.0)
        assert body["free_tmn"] == 4_000_000.0
        assert body["open_positions"] == 1
        assert body["last_heartbeat_age_sec"] is not None
        assert headers.get("Cache-Control") == "no-store"

    def test_equity_downsampled_to_500(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/equity")
        assert len(body["points"]) <= 500
        assert body["points"][0]["value"] == 5_000_000.0

    def test_positions_with_unrealized_pnl(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/positions")
        positions = body["positions"]
        assert len(positions) == 1
        pos = positions[0]
        assert pos["symbol"] == "ETHTMN"
        assert pos["last_known_bid"] is None  # no watch row for ETHTMN
        # BTCTMN is CLOSED, so its watch bid is not attached here
        assert "unrealized_pnl_tmn" not in pos

    def test_guardian_fields(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/guardian")
        assert "BTCTMN" in body["symbols"]
        w = body["symbols"]["BTCTMN"]
        assert w["best_bid"] == 99_000.0
        assert w["has_position"] is True
        assert w["distance_to_sl_pct"] == 0.5
        assert w["move_fraction_of_threshold"] == 0.27
        assert w["age_sec"] is not None
        assert body["last_heartbeat_age_sec"] is not None
        assert body["last_triggers"][0]["message"] == "price moved 2.97%"

    def test_decisions_filters(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/decisions?limit=10")
        assert len(body["decisions"]) == 2
        assert body["decisions"][0]["votes"][0]["reason"]
        _, body, _ = get(ui(seeded_db),
                         "/api/decisions?action=BUY&symbol=BTCTMN")
        assert len(body["decisions"]) == 1
        _, body, _ = get(ui(seeded_db), "/api/decisions?source=TRIGGER")
        assert len(body["decisions"]) == 1
        assert body["decisions"][0]["source"] == "TRIGGER"

    def test_decision_detail_full_raw_and_guard(self, seeded_db, ui):
        handle = ui(seeded_db)
        _, body, _ = get(handle, "/api/decisions?action=BUY")
        dec_id = body["decisions"][0]["id"]
        _, detail, _ = get(handle, "/api/decision/" + str(dec_id))
        models = detail["models"]
        assert models[0]["model"] == "m1"
        assert len(models[0]["raw"]) > 9000  # > 4k: stored and served in full
        rules = detail["guard_rules"]
        assert any(r["rule"] == "order_size" and r["passed"] for r in rules)
        assert detail["orders"][0]["client_id"] == "ag_ui_1"
        assert detail["position"]

    def test_trades_with_entry_votes(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/trades")
        trades = body["trades"]
        closed = [t for t in trades if t["status"] == "CLOSED"]
        assert len(closed) == 1
        assert closed[0]["pnl_tmn"] == 14_000.0
        assert closed[0]["close_reason"] == "TAKE_PROFIT"
        assert closed[0]["entry_votes"][0]["reason"].startswith("uptrend")

    def test_guard_rejections_net_edge_highlighted(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/guard")
        rules = {r["rule"]: r for r in body["rejections"]}
        assert rules["net_edge"]["highlight"] is True
        assert rules["thin_book"]["highlight"] is False
        assert rules["net_edge"]["count"] == 1

    def test_llm_stats(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/llm")
        stats = {m["model"]: m for m in body["models"]}
        assert stats["m1"]["calls"] == 1 and stats["m1"]["errors"] == 0
        assert stats["m2"]["errors"] == 1
        assert stats["m1"]["p95_latency_ms"] == pytest.approx(1234.0)

    def test_events_kind_filter(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/events?kind=TRIGGER")
        assert len(body["events"]) == 1
        assert body["events"][0]["kind"] == "TRIGGER"

    def test_logtail_redacts_secrets(self, seeded_db, ui, tmp_path):
        log = tmp_path / "bot.log"
        log.write_text("line one fine\nAuthorization: Bearer " + SECRET +
                       "\nx-api-key: " + SECRET + "\nline two fine\n",
                       encoding="utf-8")
        _, body, _ = get(ui(seeded_db, log_path=str(log)),
                         "/api/logtail?lines=10")
        assert SECRET not in "\n".join(body["lines"])
        assert "[redacted: possible secret]" in body["lines"]
        assert "line one fine" in body["lines"]


class TestExportsAndSafety:
    def test_exports_non_empty(self, seeded_db, ui):
        handle = ui(seeded_db)
        for path, name in (("/api/export/decisions.csv", "decisions.csv"),
                           ("/api/export/trades.csv", "trades.csv"),
                           ("/api/export/events.csv", "events.csv"),
                           ("/api/export/report.txt", "report.txt")):
            status, payload, headers = raw_get(handle, path)
            assert status == 200 and payload
            assert name in headers.get("Content-Disposition", "")
        status, payload, headers = raw_get(handle, "/api/export/decisions.csv")
        assert b"BTCTMN" in payload

    def test_report_txt_mirrors_report_sections(self, seeded_db, ui):
        _, payload, _ = raw_get(ui(seeded_db), "/api/export/report.txt")
        text = payload.decode("utf-8")
        assert "=== RUN ===" in text
        assert "net_edge" in text
        assert "PAPER (DRY_RUN)" in text

    def test_all_json_has_every_section(self, seeded_db, ui):
        _, body, _ = get(ui(seeded_db), "/api/export/all.json")
        for key in ("overview", "equity", "positions", "guardian",
                    "decisions", "trades", "guard", "llm", "events"):
            assert key in body

    def test_read_only_connection_rejects_writes(self, seeded_db):
        conn = _connect(seeded_db)
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO events (run_id, ts, level, kind, "
                             "message) VALUES (1, 'x', 'INFO', 'X', 'y')")
        finally:
            conn.close()

    def test_ui_never_modifies_the_database(self, seeded_db, ui):
        before = hashlib.sha256(open(seeded_db, "rb").read()).hexdigest()
        handle = ui(seeded_db)
        for path in ("/api/overview", "/api/decisions?limit=5",
                     "/api/trades", "/api/export/all.json", "/"):
            raw_get(handle, path)
        after = hashlib.sha256(open(seeded_db, "rb").read()).hexdigest()
        assert before == after

    def test_non_get_is_405(self, seeded_db, ui):
        handle = ui(seeded_db)
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            req = urllib.request.Request(handle.url + "/api/overview",
                                         method=method)
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 405

    def test_empty_db_returns_empty_lists(self, empty_db, ui):
        handle = ui(empty_db)
        for path in ("/api/overview", "/api/equity", "/api/positions",
                     "/api/guardian", "/api/decisions", "/api/trades",
                     "/api/guard", "/api/llm", "/api/events", "/api/logtail",
                     "/api/export/all.json"):
            status, body, _ = get(handle, path)
            assert status == 200
            assert isinstance(body, dict)
        _, body, _ = get(handle, "/api/overview")
        assert body.get("warning") == "no runs found"

    def test_missing_db_is_a_json_warning_not_a_crash(self, tmp_path, ui):
        handle = ui(str(tmp_path / "seeded.db"))  # db exists only after seed?
        # start against a genuinely missing db:
        handles = handle
        missing = str(tmp_path / "nope.db")
        h2 = start_ui_thread(missing, "127.0.0.1", 0)
        assert h2 is not None  # server starts fine; reads fail per request
        try:
            _, body, _ = get(h2, "/api/overview")
            assert "error" in body
            assert "nope.db" in body["error"]
        finally:
            h2.stop()

    def test_no_secrets_in_any_output(self, seeded_db, ui):
        handle = ui(seeded_db)
        paths = ["/api/overview", "/api/decisions?limit=5", "/api/trades",
                 "/api/events", "/api/export/report.txt",
                 "/api/export/decisions.csv", "/api/export/all.json", "/"]
        for path in paths:
            _, payload, _ = raw_get(handle, path)
            assert SECRET.encode() not in payload, f"secret leaked via {path}"


class TestFrontendFiles:
    def test_index_html_no_external_urls(self):
        html = open("ui/index.html", encoding="utf-8").read()
        assert "http://" not in html and "https://" not in html
        assert "Task Force Wallex Agent" in html
        assert "unofficial monitoring dashboard" in html

    def test_index_html_no_wallex_assets(self):
        html = open("ui/index.html", encoding="utf-8").read().lower()
        for line in html.splitlines():
            if "wallex" in line:
                # only the required product title may mention the word
                assert "task force wallex agent" in line

    def test_only_relative_resource_loads(self):
        html = open("ui/index.html", encoding="utf-8").read()
        import re
        external = re.findall(r'(?:src|href)="(?!/)[^"]+"', html)
        assert not external

    def test_style_and_app_served(self, seeded_db, ui):
        handle = ui(seeded_db)
        for path, marker in (("/style.css", "--accent"), ("/app.js", "poll")):
            status, payload, headers = raw_get(handle, path)
            assert status == 200 and marker.encode() in payload


class TestUiThread:
    def test_ephemeral_port_and_stop(self, seeded_db):
        handle = start_ui_thread(seeded_db, "127.0.0.1", 0)
        assert handle is not None
        assert ":0" not in handle.url
        try:
            _, body, _ = get(handle, "/api/overview")
            assert body["mode"] == "PAPER"
        finally:
            handle.stop()
        with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
            get(handle, "/api/overview")

    def test_port_in_use_returns_none_without_raising(self, seeded_db):
        first = start_ui_thread(seeded_db, "127.0.0.1", 0)
        assert first is not None
        try:
            second = start_ui_thread(seeded_db, "127.0.0.1",
                                      first._server.server_address[1])
            assert second is None  # OSError caught, never raises
        finally:
            first.stop()

    def test_stop_is_idempotent(self, seeded_db):
        handle = start_ui_thread(seeded_db, "127.0.0.1", 0)
        handle.stop()
        handle.stop()  # must not raise
