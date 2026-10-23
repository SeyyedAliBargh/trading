"""Tests for execution/executor.py (STEP P-B PART 1). Fakes only; no real sleeping."""
import dataclasses
import threading

import pytest

import config
import store.db as store_db
from exchange.wallex import WallexError, base_asset
from execution.executor import EntryContext, Executor
from tests.fakes import FakeClock, FakeExchange

SYMBOL = config.CONFIG.trading.SYMBOLS[0]
SYMBOL2 = config.CONFIG.trading.SYMBOLS[1]
BASE = base_asset(SYMBOL)


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "executor_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


def make_executor(temp_db, fe, **trading_over):
    cfg = config.CONFIG
    if trading_over:
        cfg = dataclasses.replace(
            cfg, trading=dataclasses.replace(cfg.trading, **trading_over))
    fc = FakeClock()
    ex = Executor(cfg, store_db, fe, clock=fc, sleep=fc.sleep, run_id=1)
    return ex, fc


def make_ctx(size_tmn=200_000.0, stop=0.015, target=0.045, decision_id=7):
    return EntryContext(decision_id=decision_id, size_tmn=size_tmn,
                        stop_pct=stop, target_pct=target, decision_uid="uid123")


def seed_position(symbol=SYMBOL, qty=1.0, entry=100.0, stop_pct=0.015,
                  target_pct=0.045, stop_price=None, target_price=None):
    return store_db.insert_position(
        run_id=1, symbol=symbol, entry_price=entry, quantity=qty,
        entry_value_tmn=entry * qty, stop_pct=stop_pct, target_pct=target_pct,
        stop_price=stop_price if stop_price is not None else entry * (1 - stop_pct),
        target_price=target_price if target_price is not None else entry * (1 + target_pct),
        dry_run=True)


def events(temp_db, kind=None):
    import sqlite3
    con = sqlite3.connect(temp_db)
    con.row_factory = sqlite3.Row
    if kind is None:
        rows = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id")]
    else:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))]
    con.close()
    return rows


class TestOpenPosition:
    def test_entry_fills_and_persists(self, temp_db):
        fe = FakeExchange([SYMBOL])
        ex, fc = make_executor(temp_db, fe)
        res = ex.open_position(SYMBOL, make_ctx())
        assert res.ok is True and res.action == "OPENED"
        assert res.entry_price == pytest.approx(101.0)
        assert res.quantity == pytest.approx(200_000 / 101, abs=1e-6)
        # position row
        positions = store_db.get_open_positions(True)
        assert len(positions) == 1
        pos = positions[0]
        assert pos["entry_price"] == pytest.approx(101.0)
        assert pos["quantity"] == pytest.approx(200_000 / 101, abs=1e-6)
        assert pos["stop_price"] == pytest.approx(101.0 * 0.985)
        assert pos["target_price"] == pytest.approx(101.0 * 1.045)
        assert pos["entry_decision_id"] == 7
        # order row linked to the decision
        order_row = store_db.get_order_by_client_id(res.client_id)
        assert order_row is not None
        assert order_row["decision_id"] == 7
        assert order_row["side"] == "BUY"
        assert order_row["dry_run"] == 1
        # balances charged
        assert fe.balances["TMN"]["free"] == pytest.approx(5_000_000 - 200_000, rel=1e-6)

    def test_entry_partial_fill_at_timeout_keeps_partial(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.partial_next = 0.4
        ex, fc = make_executor(temp_db, fe)
        res = ex.open_position(SYMBOL, make_ctx())
        assert res.ok is True
        assert res.reason == "partial_fill"
        full_qty = 200_000 / 101
        assert res.quantity == pytest.approx(0.4 * full_qty, abs=1e-6)
        pos = store_db.get_open_positions(True)[0]
        assert pos["quantity"] == pytest.approx(0.4 * full_qty, abs=1e-6)
        # SL/TP computed from the REAL average entry price
        assert pos["stop_price"] == pytest.approx(101.0 * 0.985)
        assert pos["target_price"] == pytest.approx(101.0 * 1.045)
        # polling actually slept (no real sleeping: fake clock advanced)
        assert fc.sleeps

    def test_entry_zero_fill_holds_and_logs(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.no_fill_at_create = True  # the entry order rests, nothing fills
        ex, fc = make_executor(temp_db, fe)
        res = ex.open_position(SYMBOL, make_ctx())
        assert res.ok is False and res.action == "HOLD"
        assert res.reason == "entry_unfilled"
        assert len(fe.calls) == 1  # exactly one order placed
        assert store_db.get_open_positions(True) == []
        kinds = [e["kind"] for e in events(temp_db)]
        assert "ENTRY_UNFILLED" in kinds

    def test_entry_fill_during_cancel_race(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.no_fill_at_create = True
        fe.cancel_fills_mode = True  # cancel fails because it filled
        ex, fc = make_executor(temp_db, fe)
        res = ex.open_position(SYMBOL, make_ctx())
        assert res.ok is True and res.action == "OPENED"
        full_qty = 200_000 / 101
        assert res.quantity == pytest.approx(full_qty, abs=1e-6)
        assert store_db.get_open_positions(True)
        assert "ENTRY_UNFILLED" not in [e["kind"] for e in events(temp_db)]

    def test_entry_thin_book_holds(self, temp_db):
        fe = FakeExchange([SYMBOL])
        ex, fc = make_executor(temp_db, fe)
        res = ex.open_position(SYMBOL, make_ctx(size_tmn=10_000_000.0))
        assert res.ok is False and res.reason == "thin_book"
        assert fe.calls == []
        assert store_db.get_open_positions(True) == []

    def test_entry_size_below_min_holds(self, temp_db):
        fe = FakeExchange([SYMBOL])
        ex, fc = make_executor(temp_db, fe)
        res = ex.open_position(SYMBOL, make_ctx(size_tmn=50_000.0))
        assert res.ok is False and res.reason == "size_below_min"
        assert fe.calls == []

    def test_entry_data_error_holds(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.depth_errors[SYMBOL] = WallexError("HTTP 429: rate limited", http_status=429)
        ex, fc = make_executor(temp_db, fe)
        res = ex.open_position(SYMBOL, make_ctx())
        assert res.ok is False and res.reason == "data_error"
        assert res.detail.get("http_status") == 429
        assert fe.calls == []

    def test_entry_never_raises_on_crash(self, temp_db):
        fe = FakeExchange([SYMBOL])

        def boom(symbol, ctx):
            raise RuntimeError("boom")
        ex, fc = make_executor(temp_db, fe)
        ex._open_locked = boom  # simulate an internal crash
        res = ex.open_position(SYMBOL, make_ctx())
        assert res.ok is False and res.action == "ERROR"
        assert any(e["kind"] == "EXECUTOR_CRASH" for e in events(temp_db))


class TestClosePosition:
    def test_market_sell_success_and_pnl(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.books[SYMBOL]["bids"] = [(110.0, 10_000.0, 1_100_000.0)]
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        pid = seed_position(entry=100.0, qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SL")
        assert res.ok is True and res.action == "CLOSED"
        assert res.reason == "STOP_LOSS"
        assert res.pnl_tmn == pytest.approx(10.0)  # 110 proceeds - 100 entry
        # one MARKET sell of the full position
        assert len(fe.calls) == 1
        assert fe.calls[0]["type"] == "MARKET"
        assert fe.calls[0]["side"] == "SELL"
        assert fe.calls[0]["quantity"] == pytest.approx(1.0)
        pos = store_db.get_position(pid)
        assert pos["status"] == "CLOSED"
        assert pos["close_reason"] == "STOP_LOSS"
        assert pos["pnl_tmn"] == pytest.approx(10.0)

    def test_sell_qty_comes_from_free_balance(self, temp_db):
        # buy fee was deducted from the coin: executedQty (1.0) > free (0.99)
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 0.99, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "TP")
        assert res.ok is True
        assert fe.calls[0]["quantity"] == pytest.approx(0.99)
        assert store_db.get_open_positions(True) == []

    def test_never_sells_preexisting_coins(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 5.0, "locked": 0.0}  # more than the position
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "MODEL_EXIT")
        assert res.ok is True
        assert fe.calls[0]["quantity"] == pytest.approx(1.0)  # min(free, position)
        assert fe.balances[BASE]["free"] == pytest.approx(4.0)

    def test_close_retries_transient_failures_never_gives_up(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.create_errors = [WallexError("HTTP 429", http_status=429),
                            WallexError("HTTP 503", http_status=503),
                            TimeoutError("simulated timeout")]
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SL")
        assert res.ok is True
        assert len(fe.calls) == 4  # 3 failed attempts + the successful one
        attempts = events(temp_db, "CLOSE_ATTEMPT")
        assert len(attempts) >= 4
        # one INFO attempt log per try, plus a WARN for each failure
        info_attempts = [a for a in attempts
                         if a["level"] == "INFO" and a["message"].startswith("close attempt")]
        failed_attempts = [a for a in attempts if "failed" in a["message"]]
        assert len(info_attempts) == 4
        assert len(failed_attempts) == 3
        assert store_db.get_open_positions(True) == []

    def test_close_critical_event_after_ten_failures_still_succeeds(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.create_errors = [WallexError(f"HTTP 500 #{i}", http_status=500)
                            for i in range(12)]
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SHUTDOWN")
        assert res.ok is True  # never gives up
        assert any(e["kind"] == "CRITICAL" for e in events(temp_db))
        assert store_db.get_open_positions(True) == []

    def test_market_rejected_falls_back_to_limit(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.market_rejected = {SYMBOL}
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SL")
        assert res.ok is True
        assert SYMBOL in ex._market_rejected
        assert fe.calls[0]["type"] == "MARKET"          # tried first
        assert fe.calls[1]["type"] == "LIMIT"
        assert fe.calls[1]["price"] == pytest.approx(97.0)  # bid 100 - 3 ticks
        # a second close on the same symbol goes straight to the LIMIT fallback
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        res2 = ex.close_position(SYMBOL, "TP")
        assert res2.ok is True
        assert fe.calls[2]["type"] == "LIMIT"           # no new MARKET attempt

    def test_limit_price_escalates_on_unfilled_timeout(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.market_rejected = {SYMBOL}
        fe.partial_next = 0.5  # first limit order only half-fills
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SL")
        assert res.ok is True
        limit_prices = [c["price"] for c in fe.calls if c["type"] == "LIMIT"]
        assert limit_prices[0] == pytest.approx(97.0)   # bid 100 - 3 ticks
        assert limit_prices[1] == pytest.approx(94.0)   # escalated by 3 more
        assert store_db.get_open_positions(True) == []

    def test_market_needs_price_retried_once_with_bid(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.market_needs_price = {SYMBOL}
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SL")
        assert res.ok is True
        assert len(fe.calls) == 2
        assert fe.calls[0]["type"] == "MARKET" and fe.calls[0]["price"] is None
        assert fe.calls[1]["type"] == "MARKET" and fe.calls[1]["price"] == pytest.approx(100.0)
        assert not any(c["type"] == "LIMIT" for c in fe.calls)  # no fallback needed

    def test_rate_limit_does_not_flag_market_disabled(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.create_errors = [WallexError("HTTP 429", http_status=429)]
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SL")
        assert res.ok is True
        assert SYMBOL not in ex._market_rejected  # 429 is transient
        assert all(c["type"] == "MARKET" for c in fe.calls)
        assert len(fe.calls) == 2

    def test_dust_close(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1e-9, "locked": 0.0}  # sub-precision crumb
        pid = seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        res = ex.close_position(SYMBOL, "SL")
        assert res.reason == "DUST"
        assert fe.calls == []  # nothing sellable: no order was ever placed
        pos = store_db.get_position(pid)
        assert pos["status"] == "CLOSED" and pos["close_reason"] == "DUST"

    def test_close_idempotent(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        first = ex.close_position(SYMBOL, "SL")
        assert first.ok is True
        calls_after_first = len(fe.calls)
        second = ex.close_position(SYMBOL, "SL")
        assert second.ok is True and second.reason == "no_open_position"
        assert len(fe.calls) == calls_after_first  # nothing double-acted

    def test_close_lock_timeout_returns_error(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        ex.close_lock_timeout = 0.05
        held = threading.Event()
        release = threading.Event()
        lock = ex.symbol_lock(SYMBOL)

        def hold():
            lock.acquire()
            held.set()
            release.wait(timeout=5)
            lock.release()

        holder = threading.Thread(target=hold)
        holder.start()
        try:
            assert held.wait(timeout=2)
            res = ex.close_position(SYMBOL, "SL")
            assert res.ok is False and res.reason == "lock_timeout"
        finally:
            release.set()
            holder.join(timeout=5)
        # with the lock free again the close works
        res2 = ex.close_position(SYMBOL, "SL")
        assert res2.ok is True

    def test_concurrent_close_acts_once(self, temp_db):
        fe = FakeExchange([SYMBOL])
        gate = threading.Event()
        fe.create_gate = gate
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        ex.close_lock_timeout = 5.0
        results = []

        def closer():
            results.append(ex.close_position(SYMBOL, "SL"))

        thread = threading.Thread(target=closer)
        thread.start()
        # while the first close is stuck inside create_order, the symbol is
        # flagged as closing and exactly one SELL order is ever placed
        import time as _time
        deadline = _time.time() + 2
        while not ex.is_closing(SYMBOL) and _time.time() < deadline:
            _time.sleep(0.01)
        assert ex.is_closing(SYMBOL) is True
        gate.set()
        thread.join(timeout=5)
        assert results[0].ok is True
        sell_calls = [c for c in fe.calls if c["side"] == "SELL"]
        assert len(sell_calls) == 1  # never double-acts
        assert store_db.get_open_positions(True) == []


class TestSyncWithExchange:
    def test_external_close_when_coins_gone(self, temp_db):
        fe = FakeExchange([SYMBOL])
        pid = seed_position(qty=1.0)
        ex, fc = make_executor(temp_db, fe)
        summary = ex.sync_with_exchange()
        assert summary["external_closes"] == 1
        pos = store_db.get_position(pid)
        assert pos["status"] == "CLOSED" and pos["close_reason"] == "EXTERNAL"
        assert any(e["kind"] == "POSITION_CLOSED" for e in events(temp_db))

    def test_untracked_balance_warns_never_sells(self, temp_db):
        fe = FakeExchange(config.CONFIG.trading.SYMBOLS)
        base2 = base_asset(SYMBOL2)
        fe.balances[base2] = {"free": 3.0, "locked": 0.0}
        ex, fc = make_executor(temp_db, fe)
        summary = ex.sync_with_exchange()
        assert summary["balance_warnings"] >= 1
        assert any(e["kind"] == "UNTRACKED_BALANCE" for e in events(temp_db))
        assert fe.calls == []  # never trades or sells the untracked coins

    def test_orphan_own_prefix_order_cancelled(self, temp_db):
        fe = FakeExchange([SYMBOL])
        # a resting BUY far below the market, never recorded in our DB
        fe.create_order(SYMBOL, "BUY", "LIMIT", 1.0, price=50.0,
                        client_id="ag_orphan_1")
        assert fe.orders["ag_orphan_1"]["active"] is True
        ex, fc = make_executor(temp_db, fe)
        summary = ex.sync_with_exchange()
        assert summary["orphan_cancels"] == 1
        assert fe.orders["ag_orphan_1"]["active"] is False
        assert any(e["kind"] == "ORPHAN_CANCELLED" for e in events(temp_db))

    def test_stale_db_order_cancelled(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.create_order(SYMBOL, "BUY", "LIMIT", 1.0, price=50.0,
                        client_id="ag_stale_1")
        # DB thinks this order is already finished, the exchange still lists it
        assert store_db.insert_order(run_id=1, client_id="ag_stale_1", symbol=SYMBOL,
                                     side="BUY", type="LIMIT", price=50.0,
                                     quantity=1.0, status="FILLED", dry_run=True)
        ex, fc = make_executor(temp_db, fe)
        summary = ex.sync_with_exchange()
        assert summary["orphan_cancels"] == 1
        assert fe.orders["ag_stale_1"]["active"] is False

    def test_foreign_order_untouched(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.create_order(SYMBOL, "BUY", "LIMIT", 1.0, price=50.0,
                        client_id="someone_elses_order")
        ex, fc = make_executor(temp_db, fe)
        summary = ex.sync_with_exchange()
        assert summary["foreign_untouched"] == 1
        assert fe.orders["someone_elses_order"]["active"] is True

    def test_sync_rebuilds_memory_state(self, temp_db):
        fe = FakeExchange([SYMBOL])
        ex, fc = make_executor(temp_db, fe)
        ex._market_rejected.add(SYMBOL)
        ex.sync_with_exchange()
        assert SYMBOL not in ex._market_rejected


class TestSnapshot:
    def test_snapshot_values_holdings_at_best_bid(self, temp_db):
        fe = FakeExchange([SYMBOL, SYMBOL2])
        fe.balances[BASE] = {"free": 2.0, "locked": 1.0}
        ex, fc = make_executor(temp_db, fe)
        snap = ex.snapshot()
        assert snap["total_value_tmn"] == pytest.approx(5_000_000 + 3.0 * 100.0)
        assert snap["free_tmn"] == pytest.approx(5_000_000)
        assert snap["tmn_locked"] == pytest.approx(0.0)

    def test_snapshot_error_returns_none_values(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances_error = RuntimeError("balance read failed")
        ex, fc = make_executor(temp_db, fe)
        snap = ex.snapshot()
        assert snap["total_value_tmn"] is None and snap["free_tmn"] is None

    def test_snapshot_warns_on_unknown_assets(self, temp_db):
        fe = FakeExchange([SYMBOL])
        fe.balances["DOGE"] = {"free": 100.0, "locked": 0.0}
        ex, fc = make_executor(temp_db, fe)
        snap = ex.snapshot()
        assert any(e["kind"] == "UNVALUED_ASSET" for e in events(temp_db))
        # the unknown asset is NOT part of the account value
        assert snap["total_value_tmn"] == pytest.approx(5_000_000)


class TestCloseAll:
    def test_close_all_closes_every_position(self, temp_db):
        fe = FakeExchange([SYMBOL, SYMBOL2])
        fe.balances[BASE] = {"free": 1.0, "locked": 0.0}
        fe.balances[base_asset(SYMBOL2)] = {"free": 2.0, "locked": 0.0}
        seed_position(SYMBOL, qty=1.0)
        seed_position(SYMBOL2, qty=2.0)
        ex, fc = make_executor(temp_db, fe)
        summary = ex.close_all("SHUTDOWN")
        assert summary["ok"] is True
        assert summary["remaining"] == 0
        assert store_db.get_open_positions(True) == []
        for pos in store_db.get_position(1), store_db.get_position(2):
            assert pos["close_reason"] == "SHUTDOWN"
