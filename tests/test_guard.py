"""Tests for risk/guard.py (STEP 4d PART 2). Real store on a temp db; no network."""
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
import store.db as store_db
from risk.guard import GuardResult, check_entry, exit_allowed


@pytest.fixture()
def temp_db(tmp_path):
    path = tmp_path / "guard_test.db"
    store_db.init_db(str(path))
    yield str(path)
    store_db.close_db()


def make_decision(**over):
    d = SimpleNamespace(action="BUY", confidence_mean=0.7,
                        stop_pct_median=0.015, target_pct_median=0.045,
                        agreeing_models=["m1", "m2"])
    for k, v in over.items():
        setattr(d, k, v)
    return d


def make_snapshot(**over):
    snap = {
        "best_bid": 100.0, "best_ask": 100.2, "spread_pct": 0.002,
        "asks": [(100.2, 20000.0, 2004000.0)],
        "price_precision": 0, "taker_fee": 0.003,
        "total_value_tmn": 5_000_000.0, "free_tmn": 5_000_000.0,
        "now_ts": datetime.now(timezone.utc).isoformat(),
        "run_id": None, "decision_id": None,
    }
    snap.update(over)
    return snap


def close_trade(db_path, symbol, pnl, closed_at, dry_run=True):
    """Insert + close a position with an exact closed_at (tests may touch sqlite)."""
    pid = store_db.insert_position(run_id=None, symbol=symbol, dry_run=dry_run,
                                    entry_price=100.0, quantity=1.0,
                                    entry_value_tmn=100.0)
    assert pid is not None
    assert store_db.close_position(pid, close_price=100.0, close_reason="TAKE_PROFIT",
                                   pnl_tmn=pnl, fees_tmn=0.0)
    con = sqlite3.connect(db_path)
    con.execute("UPDATE positions SET closed_at = ? WHERE id = ?", (closed_at, pid))
    con.commit()
    con.close()
    return pid


def risk_rows(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute("SELECT * FROM risk_checks ORDER BY id")]
    con.close()
    return rows


SYMBOL = config.CONFIG.trading.SYMBOLS[0]


class TestCheckEntryPass:
    def test_all_rules_pass(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        assert isinstance(res, GuardResult)
        assert res.allowed is True
        assert res.reasons == []
        assert res.size_tmn == pytest.approx(1_000_000.0)  # 0.20 * 5M
        assert res.stop_pct == pytest.approx(0.015)
        assert res.target_pct == pytest.approx(0.045)

    def test_every_rule_logged(self, temp_db):
        check_entry(store_db, config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        rows = risk_rows(temp_db)
        rules = [r["rule"] for r in rows]
        for expected in ("symbol_allowed", "entry_action", "no_open_position",
                         "max_positions", "order_size", "thin_book", "sltp_source",
                         "reward_risk", "net_edge", "daily_loss", "min_spacing",
                         "min_confidence"):
            assert expected in rules
        assert all(r["passed"] == 1 for r in rows)

    def test_deterministic_same_inputs_same_result(self, temp_db):
        a = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        b = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        assert a == b

    def test_size_capped_by_free_tmn(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(total_value_tmn=5_000_000.0, free_tmn=800_000.0))
        assert res.size_tmn == pytest.approx(800_000.0)
        assert res.allowed is True  # 800k >= min_order_tmn


class TestRuleFailures:
    def test_symbol_not_allowed(self, temp_db):
        res = check_entry(store_db, config.CONFIG, "XXXTMN", make_decision(), make_snapshot())
        assert res.allowed is False
        assert any("not in CONFIG.SYMBOLS" in r for r in res.reasons)

    def test_action_not_buy(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL,
                          make_decision(action="HOLD"), make_snapshot())
        assert res.allowed is False
        assert any("not BUY" in r for r in res.reasons)

    def test_open_position_on_symbol(self, temp_db):
        pid = store_db.insert_position(run_id=None, symbol=SYMBOL, dry_run=True)
        assert pid is not None
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        assert res.allowed is False
        assert any("already open" in r for r in res.reasons)

    def test_max_positions(self, temp_db):
        for sym in config.CONFIG.trading.SYMBOLS[1:3]:
            assert store_db.insert_position(run_id=None, symbol=sym, dry_run=True)
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        assert res.allowed is False
        assert any("open positions >=" in r for r in res.reasons)

    def test_order_size_below_min(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(total_value_tmn=400_000.0))
        # raw size 0.2*400k = 80k < 100k
        assert res.allowed is False
        assert any("min_order_tmn" in r for r in res.reasons)

    def test_free_tmn_cap_below_min(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(free_tmn=50_000.0))
        assert res.allowed is False
        assert res.size_tmn == pytest.approx(50_000.0)
        assert any("min_order_tmn" in r for r in res.reasons)

    def test_thin_book(self, temp_db):
        # ~20k TMN of ask depth cannot cover a 1M TMN order
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(asks=[(100.2, 100.0, 10020.0)]))
        assert res.allowed is False
        assert any("thin_book" in r for r in res.reasons)

    def test_thin_book_slippage_cap(self, temp_db):
        # deep book but worst level far above the slippage cap
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(asks=[(100.2, 1.0, 100.2), (600.0, 5000.0, 3_000_000.0)]))
        assert res.allowed is False
        assert any("thin_book" in r for r in res.reasons)

    def test_reward_risk_too_low(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL,
                          make_decision(target_pct_median=0.025), make_snapshot())
        assert res.allowed is False
        assert any("reward/risk" in r for r in res.reasons)

    def test_net_edge_fails(self, temp_db):
        # clamps stop->0.01, target->0.02; rr = 2.0 passes but
        # net_edge = 0.6*0.02 - 0.4*0.01 - (2*0.003 + 0.002) = 0.0 < 0.002
        res = check_entry(store_db, config.CONFIG, SYMBOL,
                          make_decision(confidence_mean=0.6, stop_pct_median=0.005,
                                        target_pct_median=0.01),
                          make_snapshot())
        assert res.allowed is False
        assert any("net edge" in r for r in res.reasons)
        assert res.stop_pct == pytest.approx(0.01)   # clamped
        assert res.target_pct == pytest.approx(0.02)  # clamped

    def test_daily_loss_cap(self, temp_db):
        now = datetime.now(timezone.utc)
        close_trade(temp_db, config.CONFIG.trading.SYMBOLS[1], pnl=-200_000.0,
                    closed_at=(now - timedelta(hours=1)).isoformat())
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(now_ts=now.isoformat()))
        assert res.allowed is False
        assert any("24h realized pnl" in r for r in res.reasons)

    def test_min_spacing(self, temp_db):
        now = datetime.now(timezone.utc)
        close_trade(temp_db, SYMBOL, pnl=0.0,
                    closed_at=(now - timedelta(seconds=60)).isoformat())
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(now_ts=now.isoformat()))
        assert res.allowed is False
        assert any("since last closed trade" in r for r in res.reasons)

    def test_spacing_ok_after_long_time(self, temp_db):
        now = datetime.now(timezone.utc)
        close_trade(temp_db, SYMBOL, pnl=0.0,
                    closed_at=(now - timedelta(seconds=1200)).isoformat())
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(now_ts=now.isoformat()))
        assert res.allowed is True

    def test_min_confidence(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL,
                          make_decision(confidence_mean=0.5), make_snapshot())
        assert res.allowed is False
        assert any("confidence" in r for r in res.reasons)


class TestNetEdgeMath:
    def test_net_edge_value_logged(self, temp_db):
        # 0.7*0.045 - 0.3*0.015 - (2*0.003 + 0.002) = 0.019
        check_entry(store_db, config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        row = [r for r in risk_rows(temp_db) if r["rule"] == "net_edge"][0]
        assert row["value"] == pytest.approx(0.019)
        assert row["threshold"] == pytest.approx(0.002)
        assert row["passed"] == 1

    def test_net_edge_threshold_semantics(self, temp_db):
        # net = 0.7*0.045 - 0.3*0.015 - (2*0.003 + spread) = 0.021 - spread
        # spread 0.018999 -> net just ABOVE 0.002 -> pass
        check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                    make_snapshot(spread_pct=0.018999, taker_fee=0.003))
        row = [r for r in risk_rows(temp_db) if r["rule"] == "net_edge"][0]
        assert row["value"] == pytest.approx(0.002001, abs=1e-6)
        assert row["passed"] == 1

    def test_net_edge_just_below_threshold_fails(self, temp_db):
        # spread 0.019001 -> net just BELOW 0.002 -> fail
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(spread_pct=0.019001, taker_fee=0.003))
        assert res.allowed is False
        assert any("net edge" in r for r in res.reasons)


class TestSltpModes:
    def test_sltp_manual_uses_config(self, temp_db):
        import dataclasses
        cfg = dataclasses.replace(
            config.CONFIG,
            trading=dataclasses.replace(config.CONFIG.trading, SLTP_MANUAL=True))
        res = check_entry(store_db, cfg, SYMBOL,
                          make_decision(stop_pct_median=None, target_pct_median=None),
                          make_snapshot())
        assert res.allowed is True
        assert res.stop_pct == pytest.approx(config.CONFIG.trading.manual_stop_pct)
        assert res.target_pct == pytest.approx(config.CONFIG.trading.manual_target_pct)

    def test_missing_medians_fail(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL,
                          make_decision(stop_pct_median=None, target_pct_median=None),
                          make_snapshot())
        assert res.allowed is False
        assert any("no stop/target source" in r for r in res.reasons)

    def test_clamping_applied(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL,
                          make_decision(stop_pct_median=0.005, target_pct_median=0.9),
                          make_snapshot())
        assert res.stop_pct == pytest.approx(config.CONFIG.risk.stop_pct_min)
        assert res.target_pct == pytest.approx(config.CONFIG.risk.target_pct_max)
        assert res.allowed is True


class TestExitsNeverBlocked:
    def test_exit_allowed_after_daily_loss_cap(self, temp_db):
        now = datetime.now(timezone.utc)
        close_trade(temp_db, SYMBOL, pnl=-200_000.0,
                    closed_at=(now - timedelta(hours=1)).isoformat())
        # entry is blocked by the loss cap...
        res = check_entry(store_db, config.CONFIG, SYMBOL, make_decision(),
                          make_snapshot(now_ts=now.isoformat()))
        assert res.allowed is False
        # ...but a stop-loss exit is ALWAYS allowed (hard rule 3)
        assert exit_allowed(store_db, config.CONFIG, SYMBOL, "STOP_LOSS") is True

    def test_all_exit_kinds_allowed(self, temp_db):
        for kind in ("STOP_LOSS", "TAKE_PROFIT", "MODEL_SELL", "MANUAL", "SHUTDOWN"):
            assert exit_allowed(store_db, config.CONFIG, SYMBOL, kind) is True

    def test_exit_logs_exempt_row(self, temp_db):
        exit_allowed(store_db, config.CONFIG, SYMBOL, "TAKE_PROFIT",
                     snapshot={"run_id": None, "decision_id": None})
        rows = risk_rows(temp_db)
        assert any(r["rule"] == "exit_TAKE_PROFIT" and r["passed"] == 1 for r in rows)


class TestRobustness:
    def test_never_raises_on_empty_snapshot(self, temp_db):
        res = check_entry(store_db, config.CONFIG, SYMBOL,
                          make_decision(confidence_mean=None,
                                        stop_pct_median=None, target_pct_median=None),
                          {})
        assert res.allowed is False
        assert res.reasons

    def test_rule_crash_counts_as_failure(self, temp_db):
        class BoomDb:
            def get_open_positions(self, dry_run):
                raise RuntimeError("db exploded")

            def realized_pnl_since(self, since, dry_run):
                return 0.0

            def get_last_closed_at(self, dry_run):
                return None

            def insert_risk_check(self, **kw):
                return store_db.insert_risk_check(**kw)

        res = check_entry(BoomDb(), config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        assert res.allowed is False
        assert any("raised" in r for r in res.reasons)

    def test_logging_failure_does_not_break_guard(self, temp_db):
        class SilentDb:
            def insert_risk_check(self, **kw):
                raise RuntimeError("log write failed")

            def get_open_positions(self, dry_run):
                return store_db.get_open_positions(dry_run)

            def realized_pnl_since(self, since, dry_run):
                return store_db.realized_pnl_since(since, dry_run)

            def get_last_closed_at(self, dry_run):
                return store_db.get_last_closed_at(dry_run)

        res = check_entry(SilentDb(), config.CONFIG, SYMBOL, make_decision(), make_snapshot())
        assert res.allowed is True  # decision unchanged; only logging failed
