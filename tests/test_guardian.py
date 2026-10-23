"""Tests for execution/guardian.py (STEP P-B PART 2). Fakes; no real sleeping."""
import dataclasses
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import store.db as store_db
from exchange.wallex import WallexError, base_asset
from execution.executor import Executor
from execution.guardian import Guardian
from risk.guard import check_entry
from tests.fakes import FakeClock, FakeExchange

SYMBOL = config.CONFIG.trading.SYMBOLS[0]
SYMBOL2 = config.CONFIG.trading.SYMBOLS[1]
BASE = base_asset(SYMBOL)
GUARDIAN_SOURCE = "execution/guardian.py"


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "guardian_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


def events(temp_db, kind=None):
    con = sqlite3.connect(temp_db)
    con.row_factory = sqlite3.Row
    if kind is None:
        rows = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id")]
    else:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))]
    con.close()
    return rows


def make_guardian(temp_db, fe, on_trigger=None, **trading_over):
    cfg = config.CONFIG
    if trading_over:
        cfg = dataclasses.replace(
            cfg, trading=dataclasses.replace(cfg.trading, **trading_over))
    fc = FakeClock()
    ex = Executor(cfg, store_db, fe, clock=fc, sleep=fc.sleep, run_id=1)
    triggers = []

    def _record_trigger(symbol):
        triggers.append(symbol)
        if on_trigger is not None:
            on_trigger(symbol)

    guard = Guardian(cfg, store_db, fe, ex, _record_trigger,
                     clock=fc, sleep=fc.sleep, run_id=1)
    return guard, ex, fc, triggers


def seed_position(symbol=SYMBOL, qty=1.0, entry=100.0, stop_price=None,
                  target_price=None):
    return store_db.insert_position(
        run_id=1, symbol=symbol, entry_price=entry, quantity=qty,
        entry_value_tmn=entry * qty, stop_pct=0.015, target_pct=0.045,
        stop_price=stop_price if stop_price is not None else 985.0,
        target_price=target_price if target_price is not None else 2000.0,
        dry_run=True)


def close_trade_at(db_path, symbol, pnl, closed_at):
    pid = store_db.insert_position(run_id=1, symbol=symbol, entry_price=100.0,
                                   quantity=1.0, entry_value_tmn=100.0,
                                   dry_run=True)
    assert store_db.close_position(pid, 100.0, "TAKE_PROFIT", pnl, 0.0)
    con = sqlite3.connect(db_path)
    con.execute("UPDATE positions SET closed_at = ? WHERE id = ?", (closed_at, pid))
    con.commit()
    con.close()


class TestSourcePurity:
    def test_no_decision_stack_imports(self):
        """SL/TP closes never touch the guard, the models or the entry module."""
        with open(GUARDIAN_SOURCE, "r", encoding="utf-8") as f:
            source = f.read()
        import_lines = [ln.strip() for ln in source.splitlines()
                        if ln.strip().startswith("import ")
                        or ln.strip().startswith("from ")]
        for line in import_lines:
            assert "risk" not in line, f"forbidden import: {line}"
            assert not line.startswith("from ai") and not line.startswith("import ai"), \
                f"forbidden import: {line}"
            assert "main" not in line, f"forbidden import: {line}"

    def test_only_allowed_imports(self):
        with open(GUARDIAN_SOURCE, "r", encoding="utf-8") as f:
            source = f.read()
        import_lines = [ln.strip() for ln in source.splitlines()
                        if ln.strip().startswith("import ")
                        or ln.strip().startswith("from ")]
        allowed = ("collections", "threading", "time", "traceback", "typing")
        for line in import_lines:
            module = line.split()[1].split(".")[0]
            assert module in allowed, f"unexpected import: {line}"


class TestStopLoss:
    def test_sl_fires_after_daily_loss_cap(self, temp_db):
        # 24h realized loss beyond the cap: the guard blocks every new entry...
        close_trade_at(temp_db, SYMBOL2, pnl=-200_000.0,
                       closed_at=datetime.now(timezone.utc).isoformat())
        decision = SimpleNamespace(action="BUY", confidence_mean=0.7,
                                   stop_pct_median=0.015, target_pct_median=0.045,
                                   agreeing_models=["m1", "m2"])
        snapshot = {"best_bid": 100.0, "best_ask": 100.2, "spread_pct": 0.002,
                    "asks": [(100.2, 20_000.0, 2_004_000.0)],
                    "price_precision": 0, "taker_fee": 0.003,
                    "total_value_tmn": 5_000_000.0, "free_tmn": 5_000_000.0,
                    "now_ts": datetime.now(timezone.utc).isoformat(),
                    "run_id": 1, "decision_id": 1}
        gr = check_entry(store_db, config.CONFIG, SYMBOL, decision, snapshot)
        assert gr.allowed is False
        assert any("24h realized pnl" in r for r in gr.reasons)

        # ...but the guardian still closes the open position on its stop
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        pid = seed_position(stop_price=105.0)  # bid 100 <= 105 -> SL
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        summary = guard.tick()
        assert summary["closed"] >= 1
        pos = store_db.get_position(pid)
        assert pos["status"] == "CLOSED"
        assert pos["close_reason"] == "STOP_LOSS"
        kinds = [e["kind"] for e in events(temp_db)]
        assert "STOP_LOSS" in kinds
        # no model was consulted on the exit path
        llm_rows = sqlite3.connect(temp_db).execute(
            "SELECT COUNT(*) FROM llm_calls").fetchone()[0]
        assert llm_rows == 0

    def test_tp_fires_on_target(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        pid = seed_position(stop_price=50.0, target_price=99.0)  # bid 100 >= 99
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()
        pos = store_db.get_position(pid)
        assert pos["close_reason"] == "TAKE_PROFIT"

    def test_no_close_when_price_inside_band(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        pid = seed_position(stop_price=90.0, target_price=110.0)  # bid 100 inside
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()
        assert store_db.get_position(pid)["status"] == "OPEN"
        assert fe.calls == []

    def test_close_in_progress_is_skipped(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        pid = seed_position(stop_price=105.0)
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        ex._closing.add(SYMBOL)  # a close is already running elsewhere
        guard.tick()
        assert store_db.get_position(pid)["status"] == "OPEN"
        assert fe.calls == []
        ex._closing.discard(SYMBOL)


class TestResilience:
    def test_price_failure_one_symbol_does_not_stop_others(self, temp_db):
        fe = FakeExchange([SYMBOL, SYMBOL2])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        fe.balances[base_asset(SYMBOL2)] = {"free": 2.0, "locked": 0.0}
        fe.depth_errors[SYMBOL] = WallexError("HTTP 500: boom", http_status=500)
        p1 = seed_position(SYMBOL, stop_price=105.0)
        p2 = seed_position(SYMBOL2, qty=2.0, stop_price=105.0)
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()
        assert store_db.get_position(p1)["status"] == "OPEN"   # unreadable, untouched
        assert store_db.get_position(p2)["status"] == "CLOSED"  # still guarded
        assert any(e["kind"] == "GUARDIAN_TICK_ERROR" for e in events(temp_db))

    def test_db_write_failing_once_is_survived(self, temp_db, monkeypatch):
        fe = FakeExchange([SYMBOL])
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        original = store_db.insert_balance_snapshot
        state = {"n": 0}

        def flaky(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("db write failed")
            return original(*args, **kwargs)

        monkeypatch.setattr(store_db, "insert_balance_snapshot", flaky)
        fc.advance(1000)  # past the balance snapshot interval
        guard.tick()  # must not raise, must log, must keep going
        assert any("balance snapshot" in e["message"]
                   for e in events(temp_db) if e["kind"] == "GUARDIAN_TICK_ERROR")
        fc.advance(1000)
        guard.tick()
        con = sqlite3.connect(temp_db)
        n = con.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0]
        con.close()
        assert n >= 1  # the retry after the failure wrote the row

    def test_heartbeat_emitted(self, temp_db):
        fe = FakeExchange([SYMBOL])
        guard, ex, fc, triggers = make_guardian(temp_db, fe,
                                                heartbeat_interval_sec=0)
        guard.tick()
        assert any(e["kind"] == "HEARTBEAT" for e in events(temp_db))

    def test_loop_restart_on_unexpected_exit(self, temp_db):
        fe = FakeExchange([SYMBOL])
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        state = {"n": 0}

        def flaky_loop():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("loop exploded")
            guard.stop()

        guard._loop = flaky_loop  # instance-level override
        guard._run()  # runs in this thread: catches, logs, restarts
        assert state["n"] == 2  # the loop really was restarted
        assert any(e["kind"] == "GUARDIAN_RESTART" for e in events(temp_db))

    def test_start_stop_join(self, temp_db):
        fe = FakeExchange([SYMBOL])
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.start()
        assert guard.is_alive is True
        guard.stop()
        guard.join(timeout=5.0)
        assert guard.is_alive is False


class TestBalanceSnapshots:
    def test_written_periodically(self, temp_db):
        fe = FakeExchange([SYMBOL])
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()  # first tick writes
        fc.advance(30)
        guard.tick()  # 30s < 60s interval: no new row
        fc.advance(40)  # now 70s since the first tick
        guard.tick()
        con = sqlite3.connect(temp_db)
        rows = con.execute("SELECT COUNT(*) FROM balance_snapshots").fetchone()[0]
        mode = con.execute("SELECT mode FROM balance_snapshots ORDER BY id "
                           "LIMIT 1").fetchone()[0]
        con.close()
        assert rows == 2
        assert mode == "paper"

    def test_paper_check_open_orders_called(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.check_open_orders_calls = 0

        def check():
            fe.check_open_orders_calls += 1

        fe.check_open_orders = check
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()
        assert fe.check_open_orders_calls == 1


class TestEventTrigger:
    def set_mid(self, fe, symbol, mid):
        fe.books[symbol] = {"bids": [(mid - 0.1, 10_000.0, 1_000_000.0)],
                            "asks": [(mid + 0.1, 10_000.0, 1_000_000.0)]}

    def test_trigger_fires_respects_cooldown(self, temp_db):
        fe = FakeExchange([SYMBOL, SYMBOL2])
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        self.set_mid(fe, SYMBOL, 101.0)
        guard.tick()  # first price point recorded, no move yet
        assert triggers == []

        fc.advance(100)
        self.set_mid(fe, SYMBOL, 104.0)  # +2.97% within the 300s window
        guard.tick()
        assert triggers == [SYMBOL]  # TRIGGER fired
        assert any(e["kind"] == "TRIGGER" for e in events(temp_db))

        fc.advance(10)
        self.set_mid(fe, SYMBOL, 106.0)  # big move, but inside the 180s cooldown
        guard.tick()
        assert triggers == [SYMBOL]

        fc.advance(200)  # cooldown over; re-establish a base point first
        self.set_mid(fe, SYMBOL, 107.0)
        guard.tick()  # only records the new base point (deque was cleared)
        assert triggers == [SYMBOL]

        fc.advance(10)  # within the rolling window, past the cooldown
        self.set_mid(fe, SYMBOL, 110.0)  # |110-107|/107 = 2.8% > 1.5%
        guard.tick()
        assert triggers == [SYMBOL, SYMBOL]

    def test_open_positions_never_trigger(self, temp_db):
        fe = FakeExchange([SYMBOL, SYMBOL2])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(SYMBOL, stop_price=1.0, target_price=10_000.0)  # never hits
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        self.set_mid(fe, SYMBOL, 101.0)
        guard.tick()
        fc.advance(100)
        self.set_mid(fe, SYMBOL, 110.0)  # ~8.9% move
        guard.tick()
        assert SYMBOL not in triggers  # open position: no trigger, no models

    def test_disabled_trigger_never_fires(self, temp_db):
        fe = FakeExchange([SYMBOL])
        guard, ex, fc, triggers = make_guardian(temp_db, fe, event_trigger_enabled=False)
        self.set_mid(fe, SYMBOL, 101.0)
        guard.tick()
        fc.advance(100)
        self.set_mid(fe, SYMBOL, 110.0)
        guard.tick()
        assert triggers == []


class TestRateLimitBackoff:
    def test_429_backs_off_one_tick(self, temp_db):
        fe = FakeExchange([SYMBOL, SYMBOL2])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        fe.balances[base_asset(SYMBOL2)] = {"free": 2.0, "locked": 0.0}
        seed_position(SYMBOL, stop_price=105.0)
        p2 = seed_position(SYMBOL2, stop_price=105.0)
        fe.depth_errors[SYMBOL] = WallexError("HTTP 429: slow down", http_status=429)
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        before = fc.now
        guard.tick()
        # 429 seen -> backoff_until is in the future
        assert guard._backoff_until > before
        assert any(e["kind"] == "RATE_LIMIT" for e in events(temp_db))
        # the other symbol was still guarded
        assert store_db.get_position(p2)["status"] == "CLOSED"
        # a clean tick resets the escalation
        fe.depth_errors.pop(SYMBOL)
        guard._consecutive_429 = 3
        guard._backoff_until = fc.now + 100
        guard.tick()
        assert guard._consecutive_429 == 0  # clean tick resets the counter
