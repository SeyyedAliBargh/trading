"""Tests for execution/guardian.py (STEP P-B PART 2 + watch observability). Fakes; no real sleeping."""
import dataclasses
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import store.db as store_db
from exchange.wallex import WallexError, base_asset
from execution.executor import Executor
from execution.guardian import Guardian, format_watch_line, watch_event_data
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


class TestWatchFormatting:
    """Pure formatters for the per-symbol guardian watch line/event."""

    def test_flat_line_format(self):
        line = format_watch_line({"symbol": "XRPTMN", "bid": 30000.0,
                                  "has_position": False, "move_pct": 0.12,
                                  "threshold_pct": 0.80, "move_frac": 0.15,
                                  "cooldown_remaining": 0.0})
        assert line.startswith("[GUARDIAN] XRPTMN bid=30000")
        assert "move(window)=+0.12%" in line
        assert "trigger 0.80%" in line
        assert "(15%)" in line
        assert "cooldown=0s" in line
        assert "no-position" in line

    def test_position_line_format(self):
        line = format_watch_line({"symbol": "BTCTMN", "bid": 98_800.0,
                                  "has_position": True, "entry": 100_000.0,
                                  "sl_price": 98_500.0, "tp_price": 104_500.0,
                                  "dist_sl_pct": 0.3, "dist_tp_pct": 5.7,
                                  "ok": True})
        assert "[GUARDIAN] BTCTMN bid=98800" in line
        assert "position entry=100000" in line
        assert "SL=98500 (+0.30%)" in line
        assert "TP=104500 (+5.70%)" in line
        assert line.endswith(" OK")

    def test_watch_event_data_fields_flat(self):
        data = {"symbol": "XRPTMN", "bid": 30000.0, "mid": 30010.0,
                "has_position": False, "move_pct": 0.5, "move_frac": 0.6,
                "cooldown_remaining": 12.0, "trigger_fired": False}
        payload = watch_event_data(data, config.CONFIG.trading)
        assert payload["symbol"] == "XRPTMN"
        assert payload["best_bid"] == 30000.0
        assert payload["mid"] == 30010.0
        assert payload["has_position"] is False
        assert payload["trigger_move_pct"] == config.CONFIG.trading.trigger_move_pct
        assert payload["trigger_window_sec"] == config.CONFIG.trading.trigger_window_sec
        assert payload["observed_move_pct_signed"] == 0.5
        assert payload["move_fraction_of_threshold"] == 0.6
        assert payload["cooldown_remaining_sec"] == 12.0
        assert payload["trigger_fired"] is False
        assert "entry" not in payload  # position fields only for positions

    def test_watch_event_data_fields_position(self):
        data = {"symbol": "BTCTMN", "bid": 99_000.0, "has_position": True,
                "entry": 100_000.0, "sl_price": 98_500.0, "tp_price": 104_500.0,
                "dist_sl_pct": 0.5, "dist_tp_pct": 5.5}
        payload = watch_event_data(data, config.CONFIG.trading)
        assert payload["has_position"] is True
        assert payload["entry"] == 100_000.0
        assert payload["sl_price"] == 98_500.0
        assert payload["tp_price"] == 104_500.0
        assert payload["distance_to_sl_pct"] == 0.5
        assert payload["distance_to_tp_pct"] == 5.5


class TestWatchLogging:
    def test_watch_events_throttled_by_clock(self, temp_db):
        fe = FakeExchange([SYMBOL, SYMBOL2])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(SYMBOL, stop_price=50.0, target_price=10_000.0)
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()                      # first tick: events fire (never seen)
        after_first = len(events(temp_db, "GUARDIAN_WATCH"))
        fc.advance(10)                    # 10s < 30s interval: no new events
        guard.tick()
        assert len(events(temp_db, "GUARDIAN_WATCH")) == after_first
        fc.advance(25)                    # 35s since the last write: fires again
        guard.tick()
        assert len(events(temp_db, "GUARDIAN_WATCH")) > after_first

    def test_watch_event_contains_required_fields(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(SYMBOL, stop_price=50.0, target_price=10_000.0)
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()
        rows = events(temp_db, "GUARDIAN_WATCH")
        assert rows
        data = json.loads(rows[0]["data_json"] or "{}")
        for key in ("symbol", "best_bid", "mid", "has_position", "trigger_move_pct",
                    "trigger_window_sec", "observed_move_pct_signed",
                    "move_fraction_of_threshold", "cooldown_remaining_sec",
                    "trigger_fired"):
            assert key in data, f"missing {key} in GUARDIAN_WATCH data"
        assert data["has_position"] is True
        for key in ("entry", "sl_price", "tp_price", "distance_to_sl_pct",
                    "distance_to_tp_pct"):
            assert key in data, f"missing {key} for an open position"

    def test_watch_console_throttled_and_verbose_per_tick(self, temp_db, capsys):
        fe = FakeExchange([SYMBOL])
        guard, ex, fc, triggers = make_guardian(temp_db, fe)  # verbosity 1
        guard.tick()
        capsys.readouterr()
        fc.advance(10)
        guard.tick()  # within the 30s console interval: no new line
        assert capsys.readouterr().out.count("[GUARDIAN]") == 0
        # verbosity 2: a compact line every tick
        guard2, ex2, fc2, _ = make_guardian(temp_db, FakeExchange([SYMBOL]),
                                            CONSOLE_VERBOSITY=2)
        guard2.tick()
        assert capsys.readouterr().out.count("[GUARDIAN]") >= 1
        fc2.advance(1)
        guard2.tick()
        assert capsys.readouterr().out.count("[GUARDIAN]") >= 1  # every tick

    def test_trigger_line_printed_loudly(self, temp_db, capsys):
        fe = FakeExchange([SYMBOL, SYMBOL2])

        def set_mid(sym, mid):
            fe.books[sym] = {"bids": [(mid - 0.1, 10_000.0, 1_000_000.0)],
                             "asks": [(mid + 0.1, 10_000.0, 1_000_000.0)]}

        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        set_mid(SYMBOL, 101.0)
        guard.tick()
        capsys.readouterr()
        fc.advance(100)
        set_mid(SYMBOL, 104.0)
        guard.tick()
        out = capsys.readouterr().out
        assert "[TRIGGER]" in out
        assert "moved 2.97%" in out
        assert "-> early decision cycle" in out

    def test_sl_close_logs_loud_line(self, temp_db, capsys):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(SYMBOL, stop_price=105.0)
        guard, ex, fc, triggers = make_guardian(temp_db, fe)
        guard.tick()
        out = capsys.readouterr().out
        assert "[CLOSE]" in out and "SL hit" in out
        assert "bid=" in out

    def test_non_ascii_never_crashes_console(self):
        import execution.guardian as g
        g._console("[GUARDIAN] non-ascii line: ...")  # Persian text, ASCII-replaced
        g._console("[GUARDIAN] \u0633\u0644\u0627\u0645")  # must not raise
        assert True


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
