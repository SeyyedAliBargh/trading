"""Tests for store/db.py.

Every test uses a temporary database file (pytest tmp_path); runtime/agent.db
is never touched. The db fixture initializes the store and closes it again.
"""

import json
import sqlite3
import threading

import pytest

import config
from store import db as store_db

EXPECTED_TABLES = {
    "runs", "api_calls", "market_snapshots", "decisions", "llm_calls",
    "risk_checks", "orders", "positions", "balance_snapshots", "events",
    "paper_account",
}

# Fixed ISO 8601 UTC boundary strings (same format the store writes).
BEFORE_ALL = "2000-01-01T00:00:00.000000+00:00"
AFTER_ALL = "2999-01-01T00:00:00.000000+00:00"


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "test.db"
    store_db.init_db(path)
    yield path
    store_db.close_db()


def _table_names(path) -> set:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        con.close()


def _scalar(path, sql, params=()):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


class TestSchema:
    def test_all_tables_exist_wal_and_reinit_is_safe(self, db):
        assert EXPECTED_TABLES <= _table_names(db)
        con = sqlite3.connect(db)
        try:
            mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            con.close()
        assert mode.lower() == "wal"
        # init_db twice on the same file is safe and keeps existing data
        assert store_db.log_event("INFO", "boot", "before") is not None
        store_db.init_db(db)
        assert store_db.log_event("INFO", "boot", "after") is not None
        assert _scalar(db, "SELECT COUNT(*) FROM events") == 2


class TestLinking:
    def test_decision_llm_risk_order_position_links(self, db):
        run_id = store_db.start_run("paper", {"dry_run": True}, git_commit="abc1234")
        assert isinstance(run_id, int)

        decision_id = store_db.insert_decision(
            run_id, symbol="BTCTMN", prompt_version="v1", input_json={"x": 1},
            votes_json=[{"model": "m1", "action": "BUY"}], final_action="BUY",
            confidence=0.9, guard_allowed=True,
        )
        llm_id = store_db.insert_llm_call(run_id, decision_id=decision_id,
                                          model="gpt-5.6-sol", raw_output="...",
                                          parsed_json={"action": "BUY"}, valid=True)
        risk_id = store_db.insert_risk_check(run_id, decision_id=decision_id,
                                             rule="max_positions", passed=True,
                                             value=1, threshold=2)
        order_id = store_db.insert_order(run_id, decision_id=decision_id, client_id="ord-1",
                                         symbol="BTCTMN", side="BUY", type="LIMIT",
                                         price=100.0, quantity=2.0, status="NEW", dry_run=True)
        position_id = store_db.insert_position(run_id, symbol="BTCTMN", entry_price=100.0,
                                               quantity=2.0, entry_value_tmn=200.0,
                                               dry_run=True, entry_decision_id=decision_id,
                                               entry_order_id=order_id)
        assert all(isinstance(x, int) for x in (decision_id, llm_id, risk_id, order_id, position_id))
        assert store_db.update_decision(decision_id, order_id=order_id,
                                        position_id=position_id, outcome="ENTERED") is True
        assert store_db.end_run(run_id) is True

        pos = store_db.get_position(position_id)
        assert pos["entry_decision_id"] == decision_id
        assert pos["entry_order_id"] == order_id
        assert pos["status"] == "OPEN"
        assert pos["opened_at"] is not None and pos["closed_at"] is None

        order_row = store_db.get_order_by_client_id("ord-1")
        assert order_row["id"] == order_id
        assert order_row["decision_id"] == decision_id
        assert order_row["dry_run"] == 1
        assert order_row["updated_at"] == order_row["ts"]

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            d = con.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
            llm = con.execute("SELECT * FROM llm_calls WHERE id = ?", (llm_id,)).fetchone()
            risk = con.execute("SELECT * FROM risk_checks WHERE id = ?", (risk_id,)).fetchone()
            run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            con.close()
        assert json.loads(d["input_json"]) == {"x": 1}
        assert json.loads(d["votes_json"]) == [{"model": "m1", "action": "BUY"}]
        assert d["order_id"] == order_id and d["position_id"] == position_id
        assert d["outcome"] == "ENTERED"
        assert llm["decision_id"] == decision_id and llm["valid"] == 1
        assert risk["decision_id"] == decision_id
        assert run["mode"] == "paper" and run["git_commit"] == "abc1234"
        assert run["ended_at"] is not None


class TestWriteFailures:
    def test_broken_connection_does_not_raise(self, db, capsys):
        store_db._conn.close()  # simulate a broken underlying connection
        assert store_db.insert_order(run_id=1, client_id="x", symbol="BTCTMN") is None
        assert store_db.update_position(1, status="CLOSED") is False
        assert store_db.close_position(1, 1.0, "MANUAL", 0.0, 0.0) is False
        assert store_db.log_event("ERROR", "db", "boom") is None
        assert store_db.set_paper_account(1.0, {}) is False
        assert "[store.db]" in capsys.readouterr().err
        # restore a working connection; unknown update fields also fail cleanly
        store_db.init_db(db)
        assert store_db.update_decision(1, not_a_column=1) is False
        assert "unknown field" in capsys.readouterr().err

    def test_update_with_no_fields_fails_cleanly(self, db, capsys):
        assert store_db.update_decision(1) is False
        assert "no fields given" in capsys.readouterr().err


class TestConcurrency:
    def test_8_threads_50_events_each_no_losses(self, db):
        errors = []

        def worker(t: int):
            try:
                for i in range(50):
                    if store_db.log_event("INFO", "tick", f"t{t}-{i}", {"t": t, "i": i}) is None:
                        errors.append(f"insert returned None at t{t}-{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert errors == []
        assert _scalar(db, "SELECT COUNT(*) FROM events") == 400


class TestPaperAccount:
    def test_created_with_configured_capital(self, db):
        acc = store_db.get_paper_account()
        assert acc is not None
        assert acc["id"] == 1
        assert acc["tmn_balance"] == pytest.approx(config.CONFIG.trading.paper_initial_capital_tmn)
        assert acc["initial_capital_tmn"] == pytest.approx(config.CONFIG.trading.paper_initial_capital_tmn)
        assert acc["assets"] == {}

    def test_persists_across_close_and_reinit(self, db):
        assert store_db.set_paper_account(123.5, {"USDT": 2.5}) is True
        store_db.close_db()
        store_db.init_db(db)
        acc = store_db.get_paper_account()
        assert acc["tmn_balance"] == pytest.approx(123.5)
        assert acc["assets"] == {"USDT": 2.5}

    def test_reset(self, db):
        assert store_db.set_paper_account(10.0, {"X": 1}) is True
        assert store_db.reset_paper_account(999.0) is True
        acc = store_db.get_paper_account()
        assert acc["tmn_balance"] == pytest.approx(999.0)
        assert acc["assets"] == {}
        assert acc["initial_capital_tmn"] == pytest.approx(999.0)


class TestPositionReads:
    def test_open_positions_pnl_and_trade_reads(self, db):
        run_id = store_db.start_run("paper", {}, git_commit="test")
        p1 = store_db.insert_position(run_id, symbol="BTCTMN", dry_run=True,
                                      entry_price=10.0, quantity=1.0)
        p2 = store_db.insert_position(run_id, symbol="BTCTMN", dry_run=True,
                                      entry_price=10.0, quantity=1.0)
        p3 = store_db.insert_position(run_id, symbol="ETHTMN", dry_run=True,
                                      entry_price=5.0, quantity=1.0)  # stays OPEN
        p4 = store_db.insert_position(run_id, symbol="BTCTMN", dry_run=False,
                                      entry_price=10.0, quantity=1.0)
        assert store_db.close_position(p1, 11.0, "TAKE_PROFIT", 100.0, 1.0) is True
        assert store_db.close_position(p2, 9.0, "STOP_LOSS", 50.0, 1.0) is True
        assert store_db.close_position(p4, 12.0, "MODEL_SELL", 250.0, 2.0) is True

        assert [p["id"] for p in store_db.get_open_positions(True)] == [p3]
        assert store_db.get_open_positions(False) == []

        assert store_db.realized_pnl_since(BEFORE_ALL, True) == pytest.approx(150.0)
        assert store_db.realized_pnl_since(BEFORE_ALL, False) == pytest.approx(250.0)
        assert store_db.realized_pnl_since(AFTER_ALL, True) == 0.0

        assert store_db.count_trades_since(BEFORE_ALL, True) == 2
        assert store_db.count_trades_since(BEFORE_ALL, False) == 1
        assert store_db.count_trades_since(AFTER_ALL, True) == 0

        closed_1 = store_db.get_position(p1)["closed_at"]
        closed_2 = store_db.get_position(p2)["closed_at"]
        closed_4 = store_db.get_position(p4)["closed_at"]
        assert store_db.get_last_trade_ts("BTCTMN", True) == max(closed_1, closed_2)
        assert store_db.get_last_trade_ts("BTCTMN", False) == closed_4
        assert store_db.get_last_trade_ts("ETHTMN", True) is None  # only an OPEN position
        assert store_db.get_last_trade_ts("NOPE", True) is None

    def test_get_last_closed_at_any_symbol(self, db):
        run_id = store_db.start_run("paper", {}, git_commit="test")
        pa = store_db.insert_position(run_id, symbol="BTCTMN", dry_run=True)
        pb = store_db.insert_position(run_id, symbol="ETHTMN", dry_run=True)
        plive = store_db.insert_position(run_id, symbol="BTCTMN", dry_run=False)
        assert store_db.close_position(pa, 11.0, "TAKE_PROFIT", 100.0, 0.0)
        assert store_db.close_position(pb, 12.0, "MODEL_SELL", -50.0, 0.0)
        assert store_db.close_position(plive, 13.0, "STOP_LOSS", -10.0, 0.0)

        closed_a = store_db.get_position(pa)["closed_at"]
        closed_b = store_db.get_position(pb)["closed_at"]
        closed_live = store_db.get_position(plive)["closed_at"]

        assert store_db.get_last_closed_at(True) == max(closed_a, closed_b)
        assert store_db.get_last_closed_at(False) == closed_live

    def test_get_last_closed_at_none_when_empty(self, db):
        assert store_db.get_last_closed_at(True) is None
        run_id = store_db.start_run("paper", {}, git_commit="test")
        p = store_db.insert_position(run_id, symbol="BTCTMN", dry_run=True)
        assert p is not None
        assert store_db.get_last_closed_at(True) is None  # OPEN only, never closed


class TestDecisionReads:
    def test_recent_decisions_per_symbol_and_mode(self, db):
        paper_run = store_db.start_run("paper", {}, git_commit="test")
        live_run = store_db.start_run("live", {}, git_commit="test")
        d1 = store_db.insert_decision(paper_run, symbol="BTCTMN", final_action="HOLD",
                                      confidence=0.55)
        d2 = store_db.insert_decision(paper_run, symbol="BTCTMN", final_action="BUY",
                                      confidence=0.72)
        d3 = store_db.insert_decision(paper_run, symbol="BTCTMN", final_action="SELL",
                                      confidence=0.66)
        d4 = store_db.insert_decision(paper_run, symbol="ETHTMN", final_action="HOLD",
                                      confidence=0.4)
        d5 = store_db.insert_decision(live_run, symbol="BTCTMN", final_action="HOLD",
                                      confidence=0.9)
        assert None not in (d1, d2, d3, d4, d5)

        # most-recent-first, symbol-filtered, mode-separated via runs
        rows = store_db.get_recent_decisions("BTCTMN", 3, "paper")
        assert [r["id"] for r in rows] == [d3, d2, d1]
        assert rows[0]["final_action"] == "SELL"
        assert rows[0]["confidence"] == pytest.approx(0.66)

        # limit respected
        assert [r["id"] for r in store_db.get_recent_decisions("BTCTMN", 1, "paper")] == [d3]
        # live decisions never leak into the paper view
        assert [r["id"] for r in store_db.get_recent_decisions("BTCTMN", 5, "live")] == [d5]
        assert [r["id"] for r in store_db.get_recent_decisions("ETHTMN", 5, "paper")] == [d4]
        assert store_db.get_recent_decisions("NOPE", 5, "paper") == []

    def test_decision_source_and_uid_columns(self, db):
        run_id = store_db.start_run("paper", {}, git_commit="test")
        d = store_db.insert_decision(run_id, symbol="BTCTMN", source="TRIGGER",
                                     decision_uid="c" * 32, final_action="HOLD",
                                     outcome="skipped_lock_busy",
                                     reason="symbol lock busy")
        assert d is not None
        row = store_db.get_recent_decisions("BTCTMN", 5, "paper")[0]
        assert row["source"] == "TRIGGER"
        assert row["decision_uid"] == "c" * 32
        assert row["outcome"] == "skipped_lock_busy"
        # both columns are updatable through the guarded update path
        assert store_db.update_decision(d, source="SCHEDULE", outcome="vote_HOLD")
        row = store_db.get_recent_decisions("BTCTMN", 5, "paper")[0]
        assert row["source"] == "SCHEDULE"
        assert row["outcome"] == "vote_HOLD"

    def test_close_reason_accepts_dust_and_external(self, db):
        run_id = store_db.start_run("paper", {}, git_commit="test")
        p1 = store_db.insert_position(run_id, symbol="BTCTMN", dry_run=True)
        p2 = store_db.insert_position(run_id, symbol="ETHTMN", dry_run=True)
        assert store_db.close_position(p1, 0.0, "DUST", -100.0, 0.0)
        assert store_db.close_position(p2, 0.0, "EXTERNAL", -50.0, 0.0)
        assert store_db.get_position(p1)["close_reason"] == "DUST"
        assert store_db.get_position(p2)["close_reason"] == "EXTERNAL"


class TestUniqueClientId:
    def test_duplicate_client_id_returns_none(self, db, capsys):
        first = store_db.insert_order(run_id=None, client_id="dup", symbol="BTCTMN")
        assert isinstance(first, int)
        assert store_db.insert_order(run_id=None, client_id="dup", symbol="BTCTMN") is None
        assert "insert_order failed" in capsys.readouterr().err
        assert store_db.get_order_by_client_id("dup")["id"] == first
