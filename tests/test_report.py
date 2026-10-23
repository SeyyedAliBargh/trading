"""Tests for scripts/report.py (STEP P-B PART 4). Seeded temp DB, read-only."""
import json
import sqlite3

import pytest

import store.db as store_db
from scripts.report import main as report_main
from scripts.report import open_read_only


@pytest.fixture()
def seeded_db(tmp_path):
    path = tmp_path / "report_test.db"
    store_db.init_db(str(path))
    config_snapshot = {"llm": {"MODELS": ["m1", "m2", "m3"]},
                       "trading": {"SYMBOLS": ["BTCTMN", "ETHTMN"]}}
    run_id = store_db.start_run("paper", config_snapshot, git_commit="test")

    # decision 1: entered a trade with per-model votes
    d1 = store_db.insert_decision(
        run_id, symbol="BTCTMN", prompt_version="v1", source="SCHEDULE",
        decision_uid="a" * 32, final_action="BUY", confidence=0.7,
        stop_pct=0.015, target_pct=0.045, guard_allowed=1, outcome="entered",
        votes_json=[
            {"model": "m1", "action": "BUY", "confidence": 0.7,
             "stop_pct": 0.015, "target_pct": 0.045, "valid": True,
             "reason": "EMA20 above EMA50 with rising volume"},
            {"model": "m2", "action": "BUY", "confidence": 0.72,
             "stop_pct": 0.016, "target_pct": 0.05, "valid": True,
             "reason": "breakout over the range high"},
            {"model": "m3", "action": "HOLD", "confidence": 0.4,
             "stop_pct": 0.01, "target_pct": 0.02, "valid": True,
             "reason": "RSI neutral"}])
    assert d1 is not None
    pid = store_db.insert_position(
        run_id=run_id, symbol="BTCTMN", entry_price=100.0, quantity=1000.0,
        entry_value_tmn=100_000.0, stop_pct=0.015, target_pct=0.045,
        stop_price=98.5, target_price=104.5, dry_run=True,
        entry_decision_id=d1)
    assert pid is not None
    assert store_db.close_position(pid, 103.0, "TAKE_PROFIT", 2_800.0, 200.0)
    # an open position (not part of the per-trade log's closed set)
    assert store_db.insert_position(
        run_id=run_id, symbol="ETHTMN", entry_price=200.0, quantity=10.0,
        entry_value_tmn=2_000.0, dry_run=True)

    # decision 2: rejected by the guard
    d2 = store_db.insert_decision(
        run_id, symbol="ETHTMN", prompt_version="v1", source="TRIGGER",
        decision_uid="b" * 32, final_action="HOLD", confidence=0.7,
        guard_allowed=0, outcome="guard_rejected",
        reason="net edge -0.01 < min 0.002",
        votes_json=[{"model": "m1", "action": "BUY", "confidence": 0.7,
                     "valid": True, "reason": "x"},
                    {"model": "m2", "action": "BUY", "confidence": 0.7,
                     "valid": True, "reason": "y"}])
    assert d2 is not None
    for _ in range(2):
        store_db.insert_risk_check(run_id, decision_id=d2, rule="net_edge",
                                   passed=False, value=-0.01, threshold=0.002,
                                   message="net edge too small")
    store_db.insert_risk_check(run_id, decision_id=d2, rule="thin_book",
                               passed=False, value=0, threshold=1,
                               message="thin book")
    store_db.insert_risk_check(run_id, decision_id=d1, rule="order_size",
                               passed=True, value=1_000_000.0,
                               threshold=100_000.0, message="")

    # llm calls with latencies and one failure
    for model, latencies in (("m1", [100.0, 200.0, 300.0]),
                             ("m2", [400.0, 500.0]),
                             ("m3", [600.0])):
        for lat in latencies:
            store_db.insert_llm_call(run_id, decision_id=d1, model=model,
                                     raw_output="{}", parsed_json={"action": "BUY"},
                                     valid=True, prompt_tokens=900,
                                     completion_tokens=100, latency_ms=lat)
    store_db.insert_llm_call(run_id, decision_id=d2, model="m1",
                             raw_output="garbage", valid=False,
                             error="no JSON object in content",
                             prompt_tokens=900, completion_tokens=0,
                             latency_ms=50.0)

    # events the report summarizes
    for kind in ("TRIGGER", "ENTRY_UNFILLED", "CLOSE_ATTEMPT", "RATE_LIMIT"):
        store_db.log_event("INFO", kind, f"{kind} happened", None, run_id)
    store_db.log_event("ERROR", "GUARDIAN_TICK_ERROR", "boom", None, run_id)
    store_db.log_event("ERROR", "CRITICAL", "10 close attempts", None, run_id)

    # equity curve: 5.0M -> 5.1M -> 4.9M -> 5.2M -> 5.05M
    for total in (5_000_000.0, 5_100_000.0, 4_900_000.0, 5_200_000.0,
                  5_050_000.0):
        store_db.insert_balance_snapshot(run_id, "paper", 4_000_000.0,
                                         {"TMN": {"free": 4_000_000.0,
                                                  "locked": 0.0}},
                                         total)
    store_db.close_db()
    yield str(path)


def test_report_prints_full_audit(seeded_db, capsys):
    code = report_main(["--db", seeded_db, "--run", "latest"])
    out = capsys.readouterr().out
    assert code == 0
    # run info
    assert "mode:          paper" in out
    assert "models:        m1, m2, m3" in out
    assert "prompt ver:    v1" in out
    # equity
    assert "max drawdown:" in out
    # decisions by action and source
    assert "by action:" in out and "BUY" in out and "HOLD" in out
    assert "by source:" in out and "SCHEDULE" in out and "TRIGGER" in out
    assert "BUY votes that reached the guard: 2" in out
    # guard rejection breakdown highlights net_edge
    assert "GUARD REJECTIONS BY RULE" in out
    assert "net_edge" in out and "<<<" in out
    assert "thin_book" in out
    # per-trade decision log with the entry votes and reasons
    assert "TRADES" in out
    assert "decision_uid=" + "a" * 32 in out
    assert "entry votes:" in out
    assert "m1" in out and "EMA20 above EMA50" in out
    assert "TAKE_PROFIT" in out
    assert "2,800.0 TMN" in out  # realized pnl
    # llm stats
    assert "LLM CALLS" in out
    assert "err%" in out and "p95_ms" in out
    # events summary
    assert "TRIGGER" in out and "ENTRY_UNFILLED" in out
    assert "CLOSE_ATTEMPT" in out and "CRITICAL" in out
    # paper honesty note
    assert "PAPER (DRY_RUN)" in out
    assert "optimistic" in out


def test_report_specific_run_id(seeded_db, capsys):
    code = report_main(["--db", seeded_db, "--run", "1"])
    assert code == 0
    assert "run id:        1" in capsys.readouterr().out


def test_report_missing_run(seeded_db, capsys):
    code = report_main(["--db", seeded_db, "--run", "999"])
    assert code == 0
    assert "No runs found" in capsys.readouterr().out


def test_report_invalid_run_arg(seeded_db):
    with pytest.raises(SystemExit):
        report_main(["--db", seeded_db, "--run", "not-an-int"])


def test_database_is_read_only(seeded_db):
    conn = open_read_only(seeded_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO events (run_id, ts, level, kind, message) "
                         "VALUES (1, 'x', 'INFO', 'X', 'y')")
    finally:
        conn.close()


def test_report_missing_db():
    with pytest.raises(SystemExit):
        open_read_only("Z:/definitely/not/here.db")
