"""Tests for main.py (STEP P-B PART 3 + observability). Fakes only; no network, no real sleeping."""
import dataclasses
import json
import logging
import sqlite3
import threading
import time as _time
from unittest.mock import MagicMock

import pytest

import config
import main as main_mod
import store.db as store_db
from exchange.wallex import WallexError, base_asset
from execution.executor import Executor
from main import Agent, Backoff, build_parser, format_decision_block, main, run
from tests.fakes import FakeClock, FakeExchange, FakeLlmClient

SYMBOL = config.CONFIG.trading.SYMBOLS[0]
BASE = base_asset(SYMBOL)


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "main_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


@pytest.fixture()
def fake_llm(monkeypatch):
    monkeypatch.setattr("main.BoxApiClient", FakeLlmClient)
    return FakeLlmClient


def make_agent(temp_db, fe=None, actions=None, **trading_over):
    cfg = config.CONFIG
    # the Agent's cfg must point at the SAME database the store uses
    cfg = dataclasses.replace(
        cfg, storage=dataclasses.replace(cfg.storage, DB_PATH=str(temp_db)))
    if trading_over:
        cfg = dataclasses.replace(
            cfg, trading=dataclasses.replace(cfg.trading, **trading_over))
    if fe is None:
        fe = FakeExchange(list(cfg.trading.SYMBOLS))
    fc = FakeClock()
    executor = Executor(cfg, store_db, fe, clock=fc, sleep=fc.sleep, run_id=1)
    agent = Agent(cfg, store_db, fe, executor, run_id=1, clock=fc, sleep=fc.sleep)
    if actions is not None:
        agent.llm.actions = actions
    return agent, executor, fe, fc


def rows(temp_db, sql, params=()):
    con = sqlite3.connect(temp_db)
    con.row_factory = sqlite3.Row
    out = [dict(r) for r in con.execute(sql, params)]
    con.close()
    return out


class TestBackoff:
    def test_backoff_math(self):
        backoff = Backoff(2.0, 60.0)
        assert backoff.delay() == 0.0
        expected = [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
        for want in expected:
            assert backoff.hit() == pytest.approx(want)
        backoff.reset()
        assert backoff.delay() == 0.0
        assert backoff.hit() == pytest.approx(2.0)  # starts over


class TestDecideSymbol:
    def test_busy_symbol_is_skipped_and_logged(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db)
        lock = executor.symbol_lock(SYMBOL)
        held = threading.Event()
        release = threading.Event()

        def hold():
            lock.acquire()
            held.set()
            release.wait(timeout=5)
            lock.release()

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert held.wait(timeout=2)
            result = agent.decide_symbol(SYMBOL, "TRIGGER")
            assert result["outcome"] == "skipped_lock_busy"
            decision = rows(temp_db, "SELECT * FROM decisions")[0]
            assert decision["source"] == "TRIGGER"
            assert decision["outcome"] == "skipped_lock_busy"
            assert decision["decision_uid"]
            events = rows(temp_db, "SELECT * FROM events WHERE kind = 'SKIPPED_BUSY'")
            assert events
        finally:
            release.set()
            holder.join(timeout=5)

    def test_rate_limited_symbol_holds_and_loop_backs_off(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db)
        fe.candles_errors[SYMBOL] = WallexError("HTTP 429: rate limited",
                                                http_status=429)
        result = agent.decide_symbol(SYMBOL, "SCHEDULE")
        assert result["action"] == "HOLD"
        assert result["outcome"] == "rate_limited"
        assert result["rate_limited"] is True
        agent.run_cycle()  # cycle sees the 429 -> loop backs off
        assert agent.backoff.delay() == pytest.approx(2.0)
        fe.candles_errors[SYMBOL] = WallexError("HTTP 429: again", http_status=429)
        agent.run_cycle()
        assert agent.backoff.delay() == pytest.approx(4.0)
        fe.candles_errors.pop(SYMBOL)
        agent.run_cycle()  # clean cycle resets the backoff
        assert agent.backoff.delay() == 0.0

    def test_data_error_holds(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db)
        fe.depth_errors[SYMBOL] = RuntimeError("book exploded")
        result = agent.decide_symbol(SYMBOL, "SCHEDULE")
        assert result["action"] == "HOLD" and result["outcome"] == "data_error"
        decision = rows(temp_db, "SELECT * FROM decisions")[0]
        assert decision["final_action"] == "HOLD"

    def test_decision_crash_is_caught(self, temp_db, fake_llm, monkeypatch):
        agent, executor, fe, fc = make_agent(temp_db)

        def boom(symbol, source, decision_uid):
            raise RuntimeError("decision exploded")

        monkeypatch.setattr(agent, "_decide_locked", boom)
        result = agent.decide_symbol(SYMBOL, "SCHEDULE")
        assert result["outcome"] == "crash"
        assert rows(temp_db, "SELECT * FROM events WHERE kind = 'CYCLE_CRASH'")

    def test_one_decision_id_links_every_row(self, temp_db, fake_llm):
        # single symbol for a deterministic cycle
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL])
        summary = agent.run_cycle()
        assert summary["rate_limited"] is False
        decisions = rows(temp_db, "SELECT * FROM decisions")
        assert len(decisions) == 1
        d = decisions[0]
        assert d["source"] == "SCHEDULE"
        assert d["final_action"] == "BUY"
        assert d["outcome"] == "entered"
        assert d["decision_uid"] and len(d["decision_uid"]) == 32  # uuid4 hex
        # every related row carries the SAME decisions-row id
        llm = rows(temp_db, "SELECT * FROM llm_calls")
        assert llm and all(call["decision_id"] == d["id"] for call in llm)
        risk = rows(temp_db, "SELECT * FROM risk_checks")
        assert risk and all(r["decision_id"] == d["id"] for r in risk)
        orders = rows(temp_db, "SELECT * FROM orders")
        assert orders and all(o["decision_id"] == d["id"] for o in orders)
        positions = rows(temp_db, "SELECT * FROM positions")
        assert positions and positions[0]["entry_decision_id"] == d["id"]
        assert positions[0]["entry_order_id"] == orders[0]["id"]
        # events carry the decision_uid of this invocation
        with_uid = [e for e in rows(temp_db, "SELECT * FROM events")
                    if e.get("data_json") and d["decision_uid"] in e["data_json"]]
        assert with_uid

    def test_guard_rejection_is_persisted(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL])
        # enormous spread -> net_edge can never clear the round-trip cost
        fe.books[SYMBOL] = {"bids": [(100.0, 10_000.0, 1_000_000.0)],
                            "asks": [(200.0, 10_000.0, 2_000_000.0)]}
        result = agent.decide_symbol(SYMBOL, "SCHEDULE")
        assert result["outcome"] == "guard_rejected"
        decision = rows(temp_db, "SELECT * FROM decisions")[0]
        assert decision["final_action"] == "HOLD"
        assert decision["guard_allowed"] == 0
        assert "net edge" in (decision["reason"] or "")  # guard message text
        failed_rules = {r["rule"] for r in rows(
            temp_db, "SELECT * FROM risk_checks WHERE passed = 0")}
        assert "net_edge" in failed_rules

    def test_hold_vote_persists_without_order(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL],
                                             actions=["HOLD", "HOLD", "HOLD"])
        result = agent.decide_symbol(SYMBOL, "SCHEDULE")
        assert result["action"] == "HOLD"
        decision = rows(temp_db, "SELECT * FROM decisions")[0]
        assert decision["final_action"] == "HOLD"
        assert decision["outcome"] == "vote_HOLD"
        assert rows(temp_db, "SELECT * FROM orders") == []
        assert fe.calls == []

    def test_exit_vote_closes_position(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL],
                                              actions=["SELL", "SELL", "HOLD"])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        pid = store_db.insert_position(
            run_id=1, symbol=SYMBOL, entry_price=100.0, quantity=1.0,
            entry_value_tmn=100.0, stop_pct=0.015, target_pct=0.045,
            stop_price=98.5, target_price=104.5, dry_run=True)
        result = agent.decide_symbol(SYMBOL, "SCHEDULE")
        assert result["action"] == "SELL"
        pos = store_db.get_position(pid)
        assert pos["status"] == "CLOSED"
        assert pos["close_reason"] == "MODEL_SELL"
        decision = rows(temp_db, "SELECT * FROM decisions")[0]
        assert decision["final_action"] == "SELL"
        # exit_allowed logged the (always-true) exemption
        assert rows(temp_db,
                    "SELECT * FROM risk_checks WHERE rule = 'exit_MODEL_SELL'")

    def test_exit_vote_disabled_never_calls_models(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL],
                                              MODELS_VOTE_ON_EXIT=False,
                                              actions=["SELL", "SELL", "SELL"])
        store_db.insert_position(run_id=1, symbol=SYMBOL, entry_price=100.0,
                                 quantity=1.0, entry_value_tmn=100.0,
                                 dry_run=True)
        result = agent.decide_symbol(SYMBOL, "SCHEDULE")
        assert result["outcome"] == "position_open_hold"
        assert agent.llm.calls == []  # models were never asked
        assert rows(temp_db, "SELECT * FROM positions WHERE status = 'OPEN'")


class TestRunLoop:
    def test_run_forever_cycles_until_stopped(self, temp_db, fake_llm):
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL],
                                              DECISION_INTERVAL_SEC=5)
        agent_stop = threading.Event()

        original_forever = agent.run_forever

        stop_after = {"cycles": 0}

        class CycleHook(logging.Handler):  # not a logging hook; simple counter
            pass

        # stop the loop after the 2nd cycle by wrapping run_cycle
        original_run_cycle = agent.run_cycle

        def run_cycle_once():
            result = original_run_cycle()
            stop_after["cycles"] += 1
            if stop_after["cycles"] >= 2:
                agent.stop()
            return result

        agent.run_cycle = run_cycle_once
        agent.run_forever()  # runs in this thread; stops itself after 2 cycles
        assert stop_after["cycles"] == 2
        assert agent.llm.calls  # models were consulted

    def test_cycle_pool_isolates_symbol_failures(self, temp_db, fake_llm):
        symbols = list(config.CONFIG.trading.SYMBOLS)
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=symbols)
        # one symbol's data is broken, the others still decide
        fe.candles_errors[symbols[0]] = RuntimeError("no candles")
        summary = agent.run_cycle()
        outcomes = {r.get("symbol"): r.get("outcome") for r in summary["results"]}
        assert outcomes[symbols[0]] == "data_error"
        for other in symbols[1:]:
            assert outcomes[other] != "worker_error"
        assert summary["rate_limited"] is False


class TestShutdown:
    def test_close_positions_false_warns_and_keeps_open(self, temp_db, fake_llm,
                                                        capsys):
        agent, executor, fe, fc = make_agent(temp_db)
        store_db.insert_position(run_id=1, symbol=SYMBOL, entry_price=100.0,
                                 quantity=1.0, entry_value_tmn=100.0,
                                 dry_run=True)
        code = agent.shutdown()
        out = capsys.readouterr().out
        assert code == 0
        assert "WARNING" in out
        assert "NOT" in out
        assert SYMBOL in out
        assert store_db.get_open_positions(True)  # stays open, as configured

    def test_close_positions_true_closes_all(self, temp_db, fake_llm, capsys):
        agent, executor, fe, fc = make_agent(temp_db,
                                             CLOSE_POSITIONS_ON_SHUTDOWN=True)
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        store_db.insert_position(run_id=1, symbol=SYMBOL, entry_price=100.0,
                                 quantity=1.0, entry_value_tmn=100.0,
                                 dry_run=True)
        code = agent.shutdown()
        assert code == 0
        assert store_db.get_open_positions(True) == []
        closed = rows(temp_db, "SELECT * FROM positions")[0]
        assert closed["close_reason"] == "SHUTDOWN"

    def test_second_interrupt_forces_exit(self, temp_db, fake_llm, capsys,
                                           monkeypatch):
        agent, executor, fe, fc = make_agent(temp_db,
                                             CLOSE_POSITIONS_ON_SHUTDOWN=True)
        store_db.insert_position(run_id=1, symbol=SYMBOL, entry_price=100.0,
                                 quantity=1.0, entry_value_tmn=100.0,
                                 dry_run=True)

        def interrupting_close_all(reason="SHUTDOWN"):
            raise KeyboardInterrupt  # the second Ctrl+C lands inside close_all

        monkeypatch.setattr(executor, "close_all", interrupting_close_all)
        code = agent.shutdown()
        out = capsys.readouterr().out
        assert code == 130
        assert "FORCED EXIT" in out
        assert SYMBOL in out  # the still-open position is printed


class TestDecisionOutput:
    """PART 1.3: decision blocks, print locking, [NEXT], file detail."""

    @staticmethod
    def _dec(**over):
        dec = {"ts": "2026-10-23T10:00:00+00:00", "symbol": "BTCTMN",
               "source": "SCHEDULE", "final_action": "HOLD",
               "outcome": "guard_rejected", "reason": "net edge too small",
               "confidence": 0.7, "decision_uid": "a" * 32, "vote_action": "BUY"}
        dec.update(over)
        return dec

    @staticmethod
    def _votes():
        return [
            {"model": "m1", "action": "BUY", "confidence": 0.7, "stop_pct": 0.015,
             "target_pct": 0.045, "reason": "uptrend with rising volume", "error": None},
            {"model": "m2", "action": "HOLD", "confidence": 0.4, "stop_pct": 0.01,
             "target_pct": 0.02, "reason": "RSI neutral, " + "x" * 250, "error": None},
        ]

    @staticmethod
    def _rules():
        return [{"rule": "net_edge", "passed": 0, "value": 0.001,
                 "threshold": 0.002, "message": "net edge 0.001 < min 0.002"},
                {"rule": "order_size", "passed": 1, "value": 500_000.0,
                 "threshold": 100_000.0, "message": ""}]

    @staticmethod
    def _llm_rows():
        return [{"model": "m1", "raw_output": "raw " + "r" * 300,
                 "latency_ms": 1234.0, "prompt_tokens": 900,
                 "completion_tokens": 80}]

    def test_block_contains_header_votes_guard_outcome(self):
        block = format_decision_block(self._dec(), self._votes(), self._rules(),
                                      self._llm_rows(), verbosity=1)
        assert block.startswith("=" * 100)
        assert "[DECISION] BTCTMN" in block
        assert "source=SCHEDULE" in block
        assert "uid=aaaaaaaa" in block  # short uid
        assert "-> HOLD (guard_rejected)" in block
        assert "model m1: BUY conf=0.7 stop=0.015 target=0.045" in block
        assert "uptrend with rising volume" in block
        # the FULL long reason is kept (wrapped across lines, nothing dropped)
        assert block.count("x") == 250
        assert "vote result: action=BUY confidence=0.7" in block
        assert "guard: REJECTED" in block
        assert "[FAIL] net_edge value=0.001 threshold=0.002" in block
        assert "order_size" not in block  # verbosity 1 shows only failed rules
        assert "outcome: guard_rejected (net edge too small)" in block
        assert "raw [m1]" not in block  # raw only at verbosity 2

    def test_block_verbosity_2_shows_all_rules_and_raw(self):
        block = format_decision_block(self._dec(), self._votes(), self._rules(),
                                      self._llm_rows(), verbosity=2)
        assert "[PASS] order_size value=500000.0" in block
        assert "raw [m1] latency=1234ms tokens=900+80" in block
        assert "r" * 90 in block  # full raw text (wrapped, nothing dropped)

    def test_block_non_ascii_reason_is_safe(self):
        votes = [{"model": "m1", "action": "BUY", "confidence": 0.7,
                  "stop_pct": 0.015, "target_pct": 0.045,
                  "reason": "\u0633\u0644\u0627\u0645 trend up", "error": None}]
        block = format_decision_block(self._dec(), votes, [], [], verbosity=1)
        assert isinstance(block, str)  # never crashes
        main_mod._print_block(block)       # console path is ASCII-replaced, no crash

    def test_print_block_never_interleaves(self, monkeypatch):
        """Parallel threads + shared lock: every block arrives as ONE piece."""
        sink = []
        monkeypatch.setattr("builtins.print", lambda *a, **k: sink.append(a[0]))

        def worker(worker_id):
            for i in range(40):
                main_mod._print_block(f"=== BLOCK w{worker_id} i{i} line2 "
                                  f"{'y' * 60} ===")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(sink) == 8 * 40
        for item in sink:
            assert item.startswith("=== BLOCK w") and item.endswith("===")
            assert "\n" not in item  # one contiguous piece, never split

    def test_next_line_printed_in_run_forever(self, temp_db, fake_llm, capsys):
        agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL],
                                             DECISION_INTERVAL_SEC=5)
        original_run_cycle = agent.run_cycle
        cycles = {"n": 0}

        def run_cycle_once():
            result = original_run_cycle()
            cycles["n"] += 1
            if cycles["n"] >= 2:
                agent.stop()
            return result

        agent.run_cycle = run_cycle_once
        agent.run_forever()
        out = capsys.readouterr().out
        assert "[NEXT] next scheduled decision in" in out

    def test_file_detail_logger_gets_full_raw_regardless_of_verbosity(
            self, temp_db, fake_llm, tmp_path, monkeypatch):
        # file-only logger: attach a captured handler
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        detail = logging.getLogger(main_mod.FILE_DETAIL_LOGGER)
        handler = Capture()
        detail.addHandler(handler)
        detail.setLevel(logging.INFO)
        detail.propagate = False
        try:
            agent, executor, fe, fc = make_agent(temp_db, SYMBOLS=[SYMBOL])
            agent.decide_symbol(SYMBOL, "SCHEDULE")
            assert captured, "file detail logger got nothing"
            blob = "\n".join(captured)
            assert "[DECISION]" in blob
            assert "model m0: BUY" in blob          # full reasons always
        finally:
            detail.removeHandler(handler)

    def test_secret_never_in_console_log_or_db(self, temp_db, fake_llm, tmp_path,
                                               monkeypatch, capsys):
        """New secrecy case: even a model echoing a secret-looking string is
        stored raw, but OUR key never appears in console, file or DB rows."""
        secret = "TEST-SECRET-BOXAPI-XYZ"
        cfg = dataclasses.replace(
            config.CONFIG,
            llm=dataclasses.replace(config.CONFIG.llm, api_key=secret))
        fe = FakeExchange([SYMBOL])
        fc = FakeClock()
        executor = Executor(cfg, store_db, fe, clock=fc, sleep=fc.sleep, run_id=1)

        class EchoClient(FakeLlmClient):
            def __init__(self, cfg=None, db=None, transport=None, actions=None):
                super().__init__(cfg, db=db, actions=actions)
                self.db = db

            def decide_all(self, input_json, models=None, decision_id=None,
                           run_id=None):
                if self.db is not None:
                    self.db.insert_llm_call(
                        run_id=run_id, decision_id=decision_id, model="echo",
                        raw_output="echoed model output", parsed_json={},
                        valid=True, prompt_tokens=1, completion_tokens=1,
                        latency_ms=1.0)
                return super().decide_all(input_json, decision_id=decision_id,
                                          run_id=run_id)

        monkeypatch.setattr("main.BoxApiClient", EchoClient)
        agent = Agent(cfg, store_db, fe, executor, run_id=1, clock=fc, sleep=fc.sleep)
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = Capture()
        logging.getLogger(main_mod.FILE_DETAIL_LOGGER).addHandler(handler)
        try:
            agent.decide_symbol(SYMBOL, "SCHEDULE")
        finally:
            logging.getLogger(main_mod.FILE_DETAIL_LOGGER).removeHandler(handler)
        out = capsys.readouterr().out
        assert secret not in out
        assert secret not in "\n".join(captured)
        con = sqlite3.connect(temp_db)
        blob = "\n".join(
            str(r[0]) for r in con.execute(
                "SELECT raw_output FROM llm_calls UNION ALL "
                "SELECT message FROM events").fetchall())
        con.close()
        assert secret not in blob


class TestOnceIntegration:
    """DRY_RUN=true: main --once through the real factory/paper path with a
    recording HTTP session -- read-only Wallex traffic, no order writes."""

    @staticmethod
    def _canned_payload(url, symbols):
        markets = {"result": {"markets": [
            {"symbol": s, "base_asset": base_asset(s), "quote_asset": "TMN",
             "price_precision": 0, "amount_precision": 8,
             "is_market_type_enable": True, "is_zero_fee": False,
             "flags": [], "quote_volume_24h": 1.0} for s in symbols]},
            "message": "ok", "success": True}
        if "/markets" in url:
            return markets
        if "/v1/depth" in url:
            return {"result": {
                "bid": [{"price": "100", "quantity": "20000", "sum": "2000000"},
                        {"price": "99", "quantity": "20000", "sum": "1980000"}],
                "ask": [{"price": "100.2", "quantity": "20000", "sum": "2004000"},
                        {"price": "100.4", "quantity": "20000", "sum": "2008000"}]},
                "message": "ok", "success": True}
        if "/v1/udf/history" in url:
            return {"s": "no_data", "t": [], "o": [], "h": [], "l": [],
                    "c": [], "v": []}
        if "/v1/account/fee" in url:
            return {"result": {s: {"makerFeeRate": "0.0025",
                                   "takerFeeRate": "0.003"} for s in symbols},
                    "message": "ok", "success": True}
        if "/v1/account/balances" in url:
            return {"result": {"balances": {"TMN": {"value": "5000000",
                                                    "locked": "0"}}},
                    "message": "ok", "success": True}
        return {"message": "ok", "success": True}

    def test_main_once_dry_run_read_only(self, tmp_path, monkeypatch):
        symbols = config.CONFIG.trading.SYMBOLS[:2]
        db_path = tmp_path / "once.db"
        log_path = tmp_path / "agent.log"
        cfg = dataclasses.replace(
            config.CONFIG,
            storage=dataclasses.replace(config.CONFIG.storage,
                                        DB_PATH=str(db_path),
                                        log_file_path=str(log_path)),
            trading=dataclasses.replace(config.CONFIG.trading,
                                        SYMBOLS=symbols, dry_run=True))
        monkeypatch.setattr(config, "CONFIG", cfg)

        wallex_calls = []

        def fake_session_request(session_self, method, url, **kwargs):
            # patched as an INSTANCE method: the first positional arg is the Session
            wallex_calls.append({"method": method, "url": url})
            payload = self._canned_payload(url, symbols)
            resp = MagicMock()
            resp.status_code = 200
            resp.text = json.dumps(payload)
            resp.headers = {}
            resp.json.return_value = payload
            return resp

        monkeypatch.setattr("requests.Session.request", fake_session_request)

        llm_content = json.dumps({"action": "BUY", "confidence": 0.7,
                                  "stop_pct": 0.015, "target_pct": 0.045,
                                  "reason": "uptrend with support"})

        class LlmResponse:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": llm_content}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        monkeypatch.setattr("requests.post",
                            lambda url, headers=None, json=None, timeout=None:
                            LlmResponse())

        code = main(["--once"])
        assert code == 0

        # 1. the Wallex session NEVER issues a write: reads only
        assert wallex_calls, "expected market-data reads"
        for call in wallex_calls:
            assert call["method"] == "GET", f"non-GET Wallex call: {call}"
            assert "/v1/account/orders" not in call["url"], \
                f"order endpoint hit in DRY_RUN: {call}"
        assert not any(c["method"] in ("POST", "DELETE", "PUT")
                       for c in wallex_calls)

        # 2. the paper run still traded: rows were written with the run's ids
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        decisions = [dict(r) for r in con.execute("SELECT * FROM decisions")]
        positions = [dict(r) for r in con.execute("SELECT * FROM positions")]
        orders = [dict(r) for r in con.execute("SELECT * FROM orders")]
        llm = [dict(r) for r in con.execute("SELECT * FROM llm_calls")]
        api = [dict(r) for r in con.execute("SELECT * FROM api_calls")]
        con.close()
        assert decisions and all(d["source"] == "SCHEDULE" for d in decisions)
        assert positions  # at least one paper position was opened
        assert all(p["dry_run"] == 1 for p in positions)
        assert orders and all(o["dry_run"] == 1 for o in orders)
        # the paper order rows carry the decision id (executor attached it)
        assert all(o["decision_id"] is not None for o in orders)
        assert llm and all(call["decision_id"] for call in llm if call["decision_id"])
        # every api_call logged against the exchange was a GET
        assert all(row["method"] == "GET" for row in api)


class TestParser:
    def test_flags(self):
        args = build_parser().parse_args(["--once"])
        assert args.once is True
        args = build_parser().parse_args([])
        assert args.once is False
        args = build_parser().parse_args(["--no-ui", "--ui-port", "9000",
                                          "--ui-host", "127.0.0.1",
                                          "--verbose"])
        assert args.no_ui is True and args.ui_port == 9000
        assert args.verbose is True and args.quiet is False

    def test_help_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--help"])
        assert exc.value.code == 0


class TestVerbosityFlags:
    def _capture_cfg(self, monkeypatch):
        captured = {}
        def fake_run(args, cfg):
            captured["cfg"] = cfg
            return 0
        monkeypatch.setattr(main_mod, "run", fake_run)
        return captured

    def test_verbose_sets_2(self, monkeypatch, tmp_path):
        captured = self._capture_cfg(monkeypatch)
        assert main(["--verbose"]) == 0
        assert captured["cfg"].trading.CONSOLE_VERBOSITY == 2

    def test_quiet_sets_0(self, monkeypatch):
        captured = self._capture_cfg(monkeypatch)
        assert main(["--quiet"]) == 0
        assert captured["cfg"].trading.CONSOLE_VERBOSITY == 0

    def test_default_keeps_config_value(self, monkeypatch):
        captured = self._capture_cfg(monkeypatch)
        assert main([]) == 0
        assert captured["cfg"].trading.CONSOLE_VERBOSITY == \
            config.CONFIG.trading.CONSOLE_VERBOSITY

    def test_verbose_and_quiet_conflict(self, monkeypatch):
        assert main(["--verbose", "--quiet"]) == 2


class TestUiIntegration:
    """Loop-mode startup: banner, --no-ui, port-in-use, shutdown stops the UI."""

    @staticmethod
    def _loop_cfg(tmp_path, monkeypatch, symbols=None):
        symbols = symbols or config.CONFIG.trading.SYMBOLS[:1]
        db_path = tmp_path / "loop.db"
        log_path = tmp_path / "agent.log"
        cfg = dataclasses.replace(
            config.CONFIG,
            storage=dataclasses.replace(config.CONFIG.storage,
                                        DB_PATH=str(db_path),
                                        log_file_path=str(log_path)),
            trading=dataclasses.replace(config.CONFIG.trading,
                                        SYMBOLS=symbols, dry_run=True))
        monkeypatch.setattr(config, "CONFIG", cfg)
        wallex_calls = []

        def fake_session_request(session_self, method, url, **kwargs):
            wallex_calls.append({"method": method, "url": url})
            payload = TestOnceIntegration._canned_payload(url, symbols)
            resp = MagicMock()
            resp.status_code = 200
            resp.text = json.dumps(payload)
            resp.headers = {}
            resp.json.return_value = payload
            return resp

        monkeypatch.setattr("requests.Session.request", fake_session_request)

        class LlmResponse:
            status_code = 200

            def json(self):
                content = json.dumps({"action": "HOLD", "confidence": 0.5,
                                      "stop_pct": 0.015, "target_pct": 0.045,
                                      "reason": "wait"})
                return {"choices": [{"message": {"content": content}}],
                        "usage": {"prompt_tokens": 10,
                                  "completion_tokens": 5}}

        monkeypatch.setattr("requests.post",
                            lambda url, headers=None, json=None, timeout=None:
                            LlmResponse())
        # the run loop exits immediately after startup
        class OnceAgent(Agent):
            def run_forever(self):
                self.stop()
        monkeypatch.setattr(main_mod, "Agent", OnceAgent)
        return cfg, str(db_path)

    @staticmethod
    def _free_port():
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_banner_shows_url_and_shutdown_stops_ui(self, tmp_path, monkeypatch,
                                                    capsys):
        self._loop_cfg(tmp_path, monkeypatch)
        port = self._free_port()
        assert main(["--ui-port", str(port)]) == 0
        out = capsys.readouterr().out
        assert f"UI: http://127.0.0.1:{port}" in out
        # the loop-mode banner also prints the mode
        assert "MODE: PAPER" in out
        # after run() returns the dashboard is stopped
        import urllib.request
        with pytest.raises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview",
                                   timeout=2)

    def test_no_ui_flag_skips_dashboard(self, tmp_path, monkeypatch, capsys):
        cfg, db_path = self._loop_cfg(tmp_path, monkeypatch)
        assert main(["--no-ui"]) == 0
        out = capsys.readouterr().out
        assert "UI: disabled (--no-ui)" in out
        con = sqlite3.connect(db_path)
        kinds = [r[0] for r in con.execute("SELECT kind FROM events").fetchall()]
        con.close()
        assert "UI_STARTED" not in kinds

    def test_port_in_use_does_not_break_startup(self, tmp_path, monkeypatch,
                                                capsys):
        import socket
        cfg, db_path = self._loop_cfg(tmp_path, monkeypatch)
        port = self._free_port()
        holder = socket.socket()
        holder.bind(("127.0.0.1", port))
        holder.listen(1)
        try:
            assert main(["--ui-port", str(port)]) == 0  # bot unaffected
            out = capsys.readouterr().out
            assert "UI: disabled (port in use)" in out
            con = sqlite3.connect(db_path)
            rows = con.execute("SELECT kind, message FROM events "
                               "WHERE kind = 'UI_DISABLED'").fetchall()
            con.close()
            assert rows and "port in use" in rows[0][1]
        finally:
            holder.close()

