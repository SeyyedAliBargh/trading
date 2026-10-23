"""Wallex trading agent -- entry point (main.py).

Usage: python main.py [--once] [--help]
  --once  run ONE decision cycle over all symbols (no guardian thread, no
          sleep loop), print a summary and exit.

Startup order (restart safety): banner -> runs row -> exchange -> executor ->
sync_with_exchange -> guardian STARTED -> decision loop. A restart after a
crash picks open positions up from the DB, syncs and guards them before any
new decision runs.

Every decision invocation has one uuid (decision_uid) plus one decisions-row
id; every related row (llm_calls, risk_checks, orders, positions, events)
carries the decisions-row id, and events carry the uuid in data_json.

Wallex 429 anywhere on the decision path -> that symbol is HOLD this cycle
and the loop backs off (start 2s, cap 60s, reset on a clean cycle). LLM
errors/timeouts already degrade to HOLD votes in ai.models / ai.vote.
"""
import argparse
import logging
import logging.handlers
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config
import store.db as store_db
from ai.models import BoxApiClient
from ai.prompt import PROMPT_VERSION, build_input
from ai.vote import tally
from data.features import build_candle_sets, compute_features
from exchange.factory import get_exchange
from exchange.wallex import WallexError
from execution.executor import EntryContext, Executor
from execution.guardian import Guardian
from risk.guard import check_entry, exit_allowed

CANDLE_1M_WINDOW_SEC = 6 * 3600
CANDLE_1H_WINDOW_SEC = 72 * 3600


class Backoff:
    """Exponential backoff for Wallex 429s: start*2^(n-1), capped; resets on success."""

    def __init__(self, start: float, cap: float):
        self.start = float(start)
        self.cap = float(cap)
        self.hits = 0

    def hit(self) -> float:
        self.hits += 1
        return self.delay()

    def reset(self) -> None:
        self.hits = 0

    def delay(self) -> float:
        if self.hits <= 0:
            return 0.0
        return min(self.cap, self.start * (2 ** (self.hits - 1)))


def setup_logging(cfg: config.Config) -> None:
    """Console + rotating file under runtime/; ASCII-safe file output."""
    log_path = Path(cfg.storage.log_file_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(
            h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        try:
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_path), maxBytes=5_000_000, backupCount=3,
                encoding="ascii", errors="replace")
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            print(f"WARNING: could not open log file {log_path}: {exc}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py", description="Wallex SPOT trading agent")
    parser.add_argument("--once", action="store_true",
                        help="run one decision cycle over all symbols and exit")
    return parser


def votes_summary(votes: List[Any]) -> List[Dict[str, Any]]:
    """Compact per-model vote records for the decisions row and the audit report."""
    out = []
    for v in votes:
        out.append({
            "model": getattr(v, "model", None),
            "action": getattr(v, "action", None),
            "confidence": getattr(v, "confidence", None),
            "stop_pct": getattr(v, "stop_pct", None),
            "target_pct": getattr(v, "target_pct", None),
            "valid": getattr(v, "valid", None),
            "reason": getattr(v, "reason", None),
            "error": getattr(v, "error", None),
        })
    return out


class Agent:
    """The decision loop: data -> models -> vote -> guard -> executor."""

    def __init__(self, cfg: config.Config, db: Any, exchange: Any,
                 executor: Executor, run_id: Optional[int],
                 clock: Any = time.time, sleep: Any = time.sleep):
        self.cfg = cfg
        self.db = db
        self.exchange = exchange
        self.executor = executor
        self.run_id = run_id
        self.clock = clock
        self.sleep = sleep
        self.llm = BoxApiClient(cfg, db=db)
        self.backoff = Backoff(cfg.trading.ratelimit_backoff_start_sec,
                               cfg.trading.ratelimit_backoff_cap_sec)
        self._stop = threading.Event()
        self._last_sync = self.clock()
        workers = max(1, len(cfg.trading.SYMBOLS))
        self._cycle_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cycle")
        self._trigger_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trigger")

    # ------------------------------------------------------------------ #
    # infra
    # ------------------------------------------------------------------ #
    def _event(self, level: str, kind: str, message: str,
               data: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.db.log_event(level, kind, message, data, run_id=self.run_id)
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        for pool in (self._cycle_pool, self._trigger_pool):
            try:
                pool.shutdown(wait=True)
            except Exception:
                pass

    def _sleep_interruptible(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        while remaining > 0.0 and not self._stop.is_set():
            chunk = min(0.5, remaining)
            self.sleep(chunk)
            remaining -= chunk

    # ------------------------------------------------------------------ #
    # trigger callback (called by the guardian on its own thread)
    # ------------------------------------------------------------------ #
    def on_trigger(self, symbol: str) -> None:
        """Schedule an out-of-cycle decision for the symbol; never blocks the guardian."""
        if self._stop.is_set():
            return
        self._trigger_pool.submit(self._decide_safe, symbol, "TRIGGER")

    def _decide_safe(self, symbol: str, source: str) -> Dict[str, Any]:
        try:
            return self.decide_symbol(symbol, source)
        except Exception as exc:
            self._event("ERROR", "CYCLE_CRASH",
                        f"decision cycle crashed for {symbol}: {exc}",
                        {"symbol": symbol, "source": source,
                         "traceback": "".join(traceback.format_exc(limit=6))})
            return {"symbol": symbol, "action": "HOLD", "outcome": "crash",
                    "rate_limited": False}

    # ------------------------------------------------------------------ #
    # decision cycle
    # ------------------------------------------------------------------ #
    def run_cycle(self) -> Dict[str, Any]:
        symbols = list(self.cfg.trading.SYMBOLS)
        futures = [self._cycle_pool.submit(self._decide_safe, sym, "SCHEDULE")
                   for sym in symbols]
        results: List[Dict[str, Any]] = []
        for future in futures:
            try:
                results.append(future.result(timeout=300))
            except Exception as exc:
                self._event("ERROR", "CYCLE_CRASH", f"cycle worker failed: {exc}", {})
                results.append({"symbol": "?", "action": "HOLD",
                                "outcome": "worker_error", "rate_limited": False,
                                "error": str(exc)})
        any_rate_limited = any(r.get("rate_limited") for r in results)
        if any_rate_limited:
            self.backoff.hit()
        else:
            self.backoff.reset()
        summary = {"results": results, "rate_limited": any_rate_limited,
                   "backoff_sec": self.backoff.delay()}
        self._event("INFO", "CYCLE_DONE", "decision cycle finished",
                    {"outcomes": {r.get("symbol"): r.get("outcome") for r in results},
                     "rate_limited": any_rate_limited})
        logging.info("cycle done: %s", {
            r.get("symbol"): f"{r.get('action')}/{r.get('outcome')}" for r in results})
        return summary

    def decide_symbol(self, symbol: str, source: str) -> Dict[str, Any]:
        """One decision for one symbol. Never raises; returns the outcome dict."""
        decision_uid = uuid.uuid4().hex
        lock = self.executor.symbol_lock(symbol)
        if not lock.acquire(blocking=False):
            # busy: skip with an event, no queueing (a decision is already running)
            self._event("INFO", "SKIPPED_BUSY",
                        f"{symbol}: skipped {source} cycle, symbol lock is held",
                        {"symbol": symbol, "source": source,
                         "decision_uid": decision_uid})
            try:
                self.db.insert_decision(run_id=self.run_id, symbol=symbol,
                                        prompt_version=PROMPT_VERSION, source=source,
                                        decision_uid=decision_uid, final_action="HOLD",
                                        outcome="skipped_lock_busy",
                                        reason="symbol lock busy")
            except Exception:
                pass
            return {"symbol": symbol, "action": "HOLD",
                    "outcome": "skipped_lock_busy", "rate_limited": False,
                    "decision_uid": decision_uid}
        try:
            try:
                return self._decide_locked(symbol, source, decision_uid)
            except Exception as exc:
                self._event("ERROR", "CYCLE_CRASH",
                            f"decision crashed for {symbol}: {exc}",
                            {"symbol": symbol, "source": source,
                             "decision_uid": decision_uid,
                             "traceback": "".join(traceback.format_exc(limit=6))})
                try:
                    self.db.insert_decision(
                        run_id=self.run_id, symbol=symbol,
                        prompt_version=PROMPT_VERSION, source=source,
                        decision_uid=decision_uid, final_action="HOLD",
                        outcome="crash", reason=str(exc))
                except Exception:
                    pass
                return {"symbol": symbol, "action": "HOLD", "outcome": "crash",
                        "rate_limited": False, "decision_uid": decision_uid}
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass

    # -- internals ---------------------------------------------------------
    def _decide_locked(self, symbol: str, source: str,
                       decision_uid: str) -> Dict[str, Any]:
        base = {"symbol": symbol, "source": source,
                "decision_uid": decision_uid, "rate_limited": False}
        # decision row FIRST so every related row can link to it
        row_id = None
        try:
            row_id = self.db.insert_decision(run_id=self.run_id, symbol=symbol,
                                             prompt_version=PROMPT_VERSION,
                                             source=source, decision_uid=decision_uid)
        except Exception as exc:
            self._event("ERROR", "DECISION_ROW_FAILED",
                        f"decision row insert failed for {symbol}: {exc}", base)
        if row_id is None:
            self._event("ERROR", "CRITICAL",
                        f"{symbol}: cannot persist the decision; cycle aborted to HOLD",
                        base)
            return {**base, "action": "HOLD", "outcome": "decision_row_failed"}

        def finish(**fields: Any) -> None:
            try:
                self.db.update_decision(row_id, **fields)
            except Exception as exc:
                self._event("ERROR", "DECISION_UPDATE_FAILED",
                            f"decision row update failed for {symbol}: {exc}", base)

        # 2. market data
        try:
            now_epoch = int(self.clock())
            candles_1m = self.exchange.get_candles_1m(
                symbol, now_epoch - CANDLE_1M_WINDOW_SEC, now_epoch)
            candles_1h = self.exchange.get_candles_1h(
                symbol, now_epoch - CANDLE_1H_WINDOW_SEC, now_epoch)
            depth = self.exchange.get_depth(symbol)
        except WallexError as exc:
            rate_limited = exc.http_status == 429
            outcome = "rate_limited" if rate_limited else "data_error"
            self._event("WARN" if rate_limited else "ERROR",
                        "RATE_LIMIT" if rate_limited else "DATA_ERROR",
                        f"market data failed for {symbol}: {exc}",
                        {**base, "http_status": exc.http_status})
            finish(final_action="HOLD", outcome=outcome, reason=str(exc))
            return {**base, "action": "HOLD", "outcome": outcome,
                    "rate_limited": rate_limited}
        except Exception as exc:
            self._event("ERROR", "DATA_ERROR",
                        f"market data failed for {symbol}: {exc}", base)
            finish(final_action="HOLD", outcome="data_error", reason=str(exc))
            return {**base, "action": "HOLD", "outcome": "data_error"}

        # features + candle sets (never raises; None fields on short data)
        try:
            sets = build_candle_sets(candles_1m, candles_1h)
            features = compute_features(candles_1m, candles_1h, depth)
        except Exception as exc:
            self._event("ERROR", "FEATURES_ERROR",
                        f"feature build failed for {symbol}: {exc}", base)
            finish(final_action="HOLD", outcome="features_error", reason=str(exc))
            return {**base, "action": "HOLD", "outcome": "features_error"}

        # account + costs
        snap = self.executor.snapshot()
        if snap.get("total_value_tmn") is None or snap.get("free_tmn") is None:
            self._event("ERROR", "SNAPSHOT_ERROR",
                        f"account snapshot failed for {symbol}", base)
            finish(final_action="HOLD", outcome="snapshot_error",
                   reason=snap.get("error", "snapshot failed"))
            return {**base, "action": "HOLD", "outcome": "snapshot_error"}

        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        if not bids or not asks:
            finish(final_action="HOLD", outcome="data_error", reason="empty book")
            return {**base, "action": "HOLD", "outcome": "data_error"}
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / mid if mid > 0 else None
        taker_fee = None
        try:
            fee_info = self.exchange.get_fee_for(symbol) or {}
            taker_fee = fee_info.get("taker_rate")
        except Exception:
            taker_fee = None
        if taker_fee is None:
            taker_fee = float(self.cfg.trading.paper_fallback_fee_rate)
        price_precision = None
        try:
            market = self.exchange.get_market_info(symbol)
            if market is not None:
                price_precision = market.price_precision
        except Exception:
            price_precision = None
        book_summary = {
            "best_bid": best_bid, "best_ask": best_ask,
            "spread_pct": spread_pct,
            "bid_qty_top5": sum(lvl[1] for lvl in bids[:5]),
            "ask_qty_top5": sum(lvl[1] for lvl in asks[:5]),
        }
        costs = {"taker_fee": taker_fee, "spread_pct": spread_pct,
                 "round_trip_cost": 2 * taker_fee + (spread_pct or 0.0)}
        guard_snapshot = {
            "best_bid": best_bid, "best_ask": best_ask,
            "spread_pct": spread_pct, "asks": asks,
            "price_precision": price_precision, "taker_fee": taker_fee,
            "total_value_tmn": snap["total_value_tmn"],
            "free_tmn": snap["free_tmn"],
            "now_ts": datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
            "run_id": self.run_id, "decision_id": row_id,
        }
        flags = {"use_news": bool(self.cfg.trading.USE_NEWS)}
        # the SAME risk values the guard clamps with (single source of truth)
        limits = {
            "stop_pct_range": [self.cfg.risk.stop_pct_min,
                                self.cfg.risk.stop_pct_max],
            "target_pct_range": [self.cfg.risk.target_pct_min,
                                 self.cfg.risk.target_pct_max],
            "min_net_edge_pct": self.cfg.risk.min_net_edge_pct,
        }
        mode = "paper" if bool(self.cfg.trading.dry_run) else "live"

        positions = []
        try:
            positions = self.db.get_open_positions(bool(self.cfg.trading.dry_run))
        except Exception as exc:
            self._event("ERROR", "POSITIONS_READ_ERROR", str(exc), base)
        my_position = next((p for p in positions if p.get("symbol") == symbol), None)

        # 3. entry path (flat)
        if my_position is None:
            try:
                recent = self.db.get_recent_decisions(symbol, 3, mode)
            except Exception:
                recent = []
            input_json = build_input(symbol, features, sets["15m"], sets["1h"],
                                     sets["4h"], book_summary, costs, None,
                                     recent, limits=limits, flags=flags)
            votes = self.llm.decide_all(input_json, decision_id=row_id,
                                        run_id=self.run_id)
            vote = tally(votes, self.cfg.llm.min_agreeing,
                         self.cfg.trading.MODELS_VOTE_ON_EXIT)
            vjson = votes_summary(votes)
            if vote.action != "BUY":
                finish(final_action=vote.action, confidence=vote.confidence_mean,
                       stop_pct=vote.stop_pct_median, target_pct=vote.target_pct_median,
                       reason="; ".join(vote.reasons), votes_json=vjson,
                       input_json=input_json, outcome=f"vote_{vote.action}")
                self._event("INFO", "DECISION",
                            f"{symbol}: {vote.action} ({source})",
                            {**base, "action": vote.action,
                             "agreeing": vote.agreeing_models})
                return {**base, "action": vote.action,
                        "outcome": f"vote_{vote.action}"}
            gr = check_entry(self.db, self.cfg, symbol, vote, guard_snapshot)
            if not gr.allowed:
                finish(final_action="HOLD", confidence=vote.confidence_mean,
                       reason=" | ".join(gr.reasons), votes_json=vjson,
                       input_json=input_json, guard_allowed=False,
                       outcome="guard_rejected")
                self._event("INFO", "GUARD_REJECTED",
                            f"{symbol}: entry rejected: {' | '.join(gr.reasons)}",
                            {**base, "rules": gr.reasons})
                return {**base, "action": "HOLD", "outcome": "guard_rejected"}
            ctx = EntryContext(decision_id=row_id, size_tmn=gr.size_tmn,
                               stop_pct=gr.stop_pct, target_pct=gr.target_pct,
                               decision_uid=decision_uid,
                               vote_summary={"models": vote.agreeing_models,
                                             "confidence": vote.confidence_mean})
            res = self.executor.open_position(symbol, ctx)
            rate_limited = res.detail.get("http_status") == 429
            if res.ok:
                finish(final_action="BUY", confidence=vote.confidence_mean,
                       stop_pct=gr.stop_pct, target_pct=gr.target_pct,
                       reason="; ".join(vote.reasons), votes_json=vjson,
                       input_json=input_json, guard_allowed=True,
                       outcome="entered", order_id=res.order_id,
                       position_id=res.position_id)
                self._event("INFO", "ENTERED",
                            f"{symbol}: opened {res.quantity} @ {res.entry_price}",
                            {**base, "position_id": res.position_id,
                             "entry_price": res.entry_price})
            else:
                finish(final_action="HOLD", confidence=vote.confidence_mean,
                       reason=res.reason, votes_json=vjson,
                       input_json=input_json, guard_allowed=True,
                       outcome=f"entry_{res.reason}")
                self._event("INFO", "ENTRY_FAILED",
                            f"{symbol}: entry not executed: {res.reason}",
                            {**base, "reason": res.reason})
            return {**base, "action": "BUY" if res.ok else "HOLD",
                    "outcome": "entered" if res.ok else f"entry_{res.reason}",
                    "rate_limited": rate_limited, "position_id": res.position_id}

        # 4. exit path (position open)
        if not self.cfg.trading.MODELS_VOTE_ON_EXIT:
            finish(final_action="HOLD", outcome="position_open_hold")
            return {**base, "action": "HOLD", "outcome": "position_open_hold"}
        entry_price = float(my_position.get("entry_price") or 0.0)
        position_summary = {
            "entry_price": entry_price,
            "quantity": my_position.get("quantity"),
            "stop_pct": my_position.get("stop_pct"),
            "target_pct": my_position.get("target_pct"),
            "unrealized_pct": ((best_bid - entry_price) / entry_price)
            if entry_price > 0 else None,
        }
        try:
            recent = self.db.get_recent_decisions(symbol, 3, mode)
        except Exception:
            recent = []
        input_json = build_input(symbol, features, sets["15m"], sets["1h"],
                                 sets["4h"], book_summary, costs, position_summary,
                                 recent, limits=limits, flags=flags)
        votes = self.llm.decide_all(input_json, decision_id=row_id,
                                    run_id=self.run_id)
        vote = tally(votes, self.cfg.llm.min_agreeing, True)
        vjson = votes_summary(votes)
        if vote.action == "SELL":
            # log-only exemption; always True (exits are never blocked)
            exit_allowed(self.db, self.cfg, symbol, "MODEL_SELL",
                         decision=vote, snapshot={"run_id": self.run_id,
                                                  "decision_id": row_id})
            res = self.executor.close_position(symbol, "MODEL_EXIT",
                                               decision_id=row_id)
            finish(final_action="SELL", confidence=vote.confidence_mean,
                   reason="; ".join(vote.reasons), votes_json=vjson,
                   input_json=input_json, position_id=my_position.get("id"),
                   outcome="closed" if res.ok else f"close_failed_{res.reason}")
            self._event("INFO", "MODEL_EXIT",
                        f"{symbol}: model SELL executed ({res.reason})",
                        {**base, "pnl_tmn": res.pnl_tmn})
            return {**base, "action": "SELL",
                    "outcome": "closed" if res.ok else f"close_failed_{res.reason}"}
        finish(final_action=vote.action or "HOLD",
               confidence=vote.confidence_mean, reason="; ".join(vote.reasons),
               votes_json=vjson, input_json=input_json,
               outcome="position_open_hold")
        return {**base, "action": vote.action or "HOLD",
                "outcome": "position_open_hold"}

    # ------------------------------------------------------------------ #
    # loop + shutdown
    # ------------------------------------------------------------------ #
    def run_forever(self) -> None:
        t = self.cfg.trading
        while not self._stop.is_set():
            started = self.clock()
            try:
                self.run_cycle()
            except Exception as exc:
                self._event("ERROR", "CYCLE_CRASH",
                            f"cycle crashed: {exc}",
                            {"traceback": "".join(traceback.format_exc(limit=6))})
            if self.clock() - self._last_sync >= float(t.sync_interval_sec):
                self._last_sync = self.clock()
                try:
                    self.executor.sync_with_exchange()
                except Exception as exc:
                    self._event("ERROR", "SYNC_ERROR", f"sync failed: {exc}", {})
            delay = max(0.0, float(t.DECISION_INTERVAL_SEC) - (self.clock() - started))
            delay = max(delay, self.backoff.delay())
            self._sleep_interruptible(delay)

    def shutdown(self) -> int:
        """Stop scheduling, close or warn, return an exit code. A second
        KeyboardInterrupt here forces the exit and prints what is still open."""
        self.stop()
        if self.cfg.trading.CLOSE_POSITIONS_ON_SHUTDOWN:
            try:
                while True:
                    remaining = self.executor.open_symbols()
                    if not remaining:
                        return 0
                    self._event("INFO", "SHUTDOWN_CLOSE",
                                f"shutdown: closing {len(remaining)} position(s): "
                                f"{', '.join(remaining)}", {"symbols": remaining})
                    self.executor.close_all("SHUTDOWN")
                    self._sleep_interruptible(
                        float(self.cfg.trading.close_retry_backoff_sec))
            except KeyboardInterrupt:
                still = self.executor.open_symbols()
                print("=" * 72)
                print("  FORCED EXIT (second interrupt). STILL OPEN, NOT CLOSED:")
                for symbol in still:
                    print(f"    - {symbol}")
                print("=" * 72)
                self._event("ERROR", "SHUTDOWN_FORCED",
                            f"forced exit with open positions: {still}",
                            {"symbols": still})
                return 130
        still = self.executor.open_symbols()
        if still:
            print("=" * 72)
            print("  WARNING: POSITIONS REMAIN OPEN AND ARE **NOT** BEING CLOSED:")
            for symbol in still:
                print(f"    - {symbol}")
            print("  Once this process exits they are NOT guarded any more")
            print("  (no stop-loss / take-profit checks will run).")
            print("=" * 72)
            self._event("WARN", "SHUTDOWN_OPEN_POSITIONS",
                        f"exiting with open positions: {still}",
                        {"symbols": still})
        return 0


def run(args: argparse.Namespace, cfg: config.Config) -> int:
    store_db.init_db(cfg.storage.DB_PATH)
    mode = "paper" if cfg.trading.dry_run else "live"
    try:
        run_id = store_db.start_run(mode, config.config_snapshot(cfg))
    except Exception:
        run_id = None
    if run_id is None:
        print("FATAL: could not create a runs row; not trading.", file=sys.stderr)
        store_db.close_db()
        return 1
    print(config.print_mode_banner(cfg))
    if not cfg.trading.dry_run:
        print("=" * 72)
        print("  !!! LIVE-MONEY MODE -- REAL ORDERS, REAL TOMAN AT RISK !!!")
        print("=" * 72)
    exchange = get_exchange(cfg, cfg.storage.DB_PATH)
    executor = Executor(cfg, store_db, exchange, run_id=run_id)
    sync_summary = executor.sync_with_exchange()
    logging.info("startup sync: %s", sync_summary)
    agent = Agent(cfg, store_db, exchange, executor, run_id)
    exit_code = 0
    try:
        if args.once:
            summary = agent.run_cycle()
            print("--once summary:")
            for result in summary["results"]:
                print(f"  {result.get('symbol')}: action={result.get('action')} "
                      f"outcome={result.get('outcome')}")
        else:
            # guardian FIRST: restart safety (guard positions before deciding)
            guardian = Guardian(cfg, store_db, exchange, executor, agent.on_trigger,
                                run_id=run_id)
            guardian.start()
            try:
                agent.run_forever()
            except KeyboardInterrupt:
                logging.info("interrupt received; shutting down")
            try:
                exit_code = agent.shutdown()
            except KeyboardInterrupt:
                exit_code = 130
                print("FORCED EXIT.", file=sys.stderr)
            finally:
                guardian.stop()
                guardian.join(timeout=10.0)
    finally:
        agent.close()
        try:
            store_db.end_run(run_id)
        except Exception:
            pass
        store_db.close_db()
    return exit_code


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config.CONFIG
    setup_logging(cfg)
    return run(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
