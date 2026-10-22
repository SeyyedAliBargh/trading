"""Entry guard (risk/guard.py).

The LLMs decide; the guard only vetoes (AGENTS.md hard rule 1 and 11).
check_entry() runs every rule, logs EVERY rule outcome to the store's
risk_checks table (rule, passed, value, threshold, message) and returns a
GuardResult. A rule crash counts as a FAILED rule: any error, timeout or bad
input on the decision path means HOLD, never an entry (hard rule 2).

Exits are exempt: exit_allowed() always returns True for stop-loss,
take-profit, shutdown and model exits (hard rule 3) -- it only logs.

Pure and deterministic: no network, no clocks except the instant passed in
via snapshot["now_ts"] (falls back to wall-clock UTC when absent), no writes
except risk_checks log rows. `db` is the store module (store.db) or any
object with the same read/log functions, so tests can inject a temp store.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from exchange import book

DAY = timedelta(hours=24)


@dataclass(frozen=True)
class GuardResult:
    """Outcome of check_entry (frozen: callers cannot mutate an audit result)."""
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    stop_pct: Optional[float] = None
    target_pct: Optional[float] = None
    size_tmn: Optional[float] = None


def _now(now_ts: Optional[str]) -> datetime:
    if now_ts:
        try:
            dt = datetime.fromisoformat(now_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)


class _Audit:
    """Collects rule outcomes; logging failures never break the guard."""

    def __init__(self, db: Any, run_id: Optional[int], decision_id: Optional[int]):
        self.db = db
        self.run_id = run_id
        self.decision_id = decision_id
        self.failures: List[str] = []
        self.results: List[Dict[str, Any]] = []

    def log(self, rule: str, passed: bool, value: Optional[float] = None,
            threshold: Optional[float] = None, message: str = "") -> bool:
        if not passed:
            self.failures.append(message or rule)
        self.results.append({"rule": rule, "passed": bool(passed), "value": value,
                             "threshold": threshold, "message": message})
        try:
            self.db.insert_risk_check(
                run_id=self.run_id, decision_id=self.decision_id, rule=rule,
                passed=bool(passed), value=value, threshold=threshold, message=message,
            )
        except Exception:
            # Logging must never crash the bot (AGENTS.md hard rule 9).
            pass
        return passed

    def safe(self, rule: str, fn, message: str = "") -> bool:
        """Run a rule callable; an exception inside counts as a failed rule."""
        try:
            return fn()
        except Exception as exc:
            return self.log(rule, False, message=message or f"{rule} raised: {exc}")


def _sltp(cfg: Any, decision: Any) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """Stop/target selection + clamping. Returns (stop, target, error)."""
    r, t = cfg.risk, cfg.trading
    if t.SLTP_MANUAL:
        stop_raw, target_raw = t.manual_stop_pct, t.manual_target_pct
    else:
        stop_raw = getattr(decision, "stop_pct_median", None)
        target_raw = getattr(decision, "target_pct_median", None)
    if stop_raw is None or target_raw is None:
        return None, None, "no stop/target source (SLTP_MANUAL off and no model medians)"
    stop = _clamp(float(stop_raw), r.stop_pct_min, r.stop_pct_max)
    target = _clamp(float(target_raw), r.target_pct_min, r.target_pct_max)
    return stop, target, None


def check_entry(db: Any, cfg: Any, symbol: str, decision: Any, snapshot: Dict[str, Any]) -> GuardResult:
    """Vet a proposed ENTRY (BUY). Evaluates every rule, logs each one to
    risk_checks and returns GuardResult(allowed, reasons, stop_pct, target_pct, size_tmn).

    `decision` is the VoteResult of this cycle (needs .action, .confidence_mean,
    .stop_pct_median, .target_pct_median).
    `snapshot` carries the market/account state gathered by the decision loop:
      required: best_bid, best_ask, spread_pct, asks (levels for
                book.entry_limit_price), price_precision, taker_fee,
                total_value_tmn (free+locked TMN + holdings at best bid),
                free_tmn
      optional: now_ts (ISO; makes the check deterministic), run_id, decision_id
    """
    audit = _Audit(db, snapshot.get("run_id"), snapshot.get("decision_id"))
    r, t = cfg.risk, cfg.trading
    dry_run = bool(t.dry_run)
    now_dt = _now(snapshot.get("now_ts"))

    # 1. symbol must be configured -------------------------------------------
    def rule_symbol():
        ok = symbol in t.SYMBOLS
        return audit.log("symbol_allowed", ok, value=float(ok), threshold=1.0,
                         message=f"symbol {symbol!r} not in CONFIG.SYMBOLS" if not ok else "")

    # 2. a BUY must have been voted ------------------------------------------
    def rule_action():
        action = getattr(decision, "action", None)
        ok = action == "BUY"
        return audit.log("entry_action", ok, message=f"vote was {action!r}, not BUY" if not ok else "")

    # 3./4. positions ----------------------------------------------------------
    def rule_no_position():
        positions = db.get_open_positions(dry_run)
        on_symbol = [p for p in positions if p.get("symbol") == symbol]
        ok = not on_symbol
        return audit.log("no_open_position", ok, value=float(len(on_symbol)), threshold=0.0,
                         message=f"position already open on {symbol}" if not ok else "")

    def rule_max_positions():
        positions = db.get_open_positions(dry_run)
        ok = len(positions) < r.max_positions
        return audit.log("max_positions", ok, value=float(len(positions)),
                         threshold=float(r.max_positions),
                         message=f"{len(positions)} open positions >= max {r.max_positions}" if not ok else "")

    # 5. order size -----------------------------------------------------------
    size: Optional[float] = None

    def rule_size():
        nonlocal size
        total = snapshot.get("total_value_tmn")
        free = snapshot.get("free_tmn")
        if total is None or free is None:
            return audit.log("order_size", False,
                             message="missing total_value_tmn/free_tmn in snapshot")
        raw = r.per_trade_fraction * float(total)
        size = min(raw, float(free))  # capped by spendable TMN
        ok = size >= r.min_order_tmn
        return audit.log("order_size", ok, value=size, threshold=float(r.min_order_tmn),
                         message=f"size {size:.0f} TMN < min_order_tmn {r.min_order_tmn:.0f}"
                         if not ok else "")

    # 6. book depth / entry price ----------------------------------------------
    def rule_thin_book():
        asks = snapshot.get("asks") or []
        if size is None or not asks:
            return audit.log("thin_book", False, value=0.0,
                             message="no size or empty ask book")
        entry_price = book.entry_limit_price(
            asks, size, t.entry_max_slippage_pct, t.entry_limit_ticks_above,
            snapshot.get("price_precision"),
        )
        best_ask = snapshot.get("best_ask")
        cap = float(best_ask) * (1 + t.entry_max_slippage_pct) if best_ask else None
        ok = entry_price is not None
        return audit.log("thin_book", ok, value=entry_price, threshold=cap,
                         message="thin_book: depth or slippage cap cannot cover the order"
                         if not ok else "")

    # 7. stop/target selection + clamping ----------------------------------------
    stop: Optional[float] = None
    target: Optional[float] = None

    def rule_sltp():
        nonlocal stop, target
        stop, target, err = _sltp(cfg, decision)
        if err:
            return audit.log("sltp_source", False, message=err)
        audit.log("sltp_source", True, value=stop, threshold=target,
                  message="stop/target selected and clamped to config ranges")
        return True

    # 8. reward/risk --------------------------------------------------------------
    def rule_reward_risk():
        if stop is None or target is None:
            return audit.log("reward_risk", False, message="no stop/target to compare")
        if stop <= 0:
            return audit.log("reward_risk", False, message=f"stop_pct {stop} <= 0")
        ratio = target / stop
        ok = ratio >= r.min_reward_risk
        return audit.log("reward_risk", ok, value=ratio, threshold=float(r.min_reward_risk),
                         message=f"reward/risk {ratio:.3f} < min {r.min_reward_risk}" if not ok else "")

    # 9. net edge after costs --------------------------------------------------------
    def rule_net_edge():
        p = getattr(decision, "confidence_mean", None)
        taker = snapshot.get("taker_fee")
        spread = snapshot.get("spread_pct")
        if p is None or taker is None or spread is None or stop is None or target is None:
            return audit.log("net_edge", False,
                             message="missing confidence/taker_fee/spread_pct/stop/target")
        round_trip = 2 * float(taker) + float(spread)
        net = float(p) * target - (1 - float(p)) * stop - round_trip
        ok = net >= r.min_net_edge_pct
        return audit.log("net_edge", ok, value=net, threshold=float(r.min_net_edge_pct),
                         message=f"net edge {net:.5f} < min {r.min_net_edge_pct} "
                         f"(round_trip_cost {round_trip:.5f})" if not ok else "")

    # 10. rolling 24h loss cap ---------------------------------------------------------
    def rule_daily_loss():
        since = (now_dt - DAY).isoformat()
        pnl = float(db.realized_pnl_since(since, dry_run))
        limit = -r.max_daily_loss_fraction * r.initial_capital_tmn
        ok = pnl > limit
        return audit.log("daily_loss", ok, value=pnl, threshold=limit,
                         message=f"24h realized pnl {pnl:.0f} <= cap {limit:.0f} TMN"
                         if not ok else "")

    # 11. minimum spacing between trades --------------------------------------------------
    def rule_spacing():
        last = db.get_last_closed_at(dry_run)
        if last is None:
            return audit.log("min_spacing", True, threshold=float(r.min_seconds_between_trades),
                             message="no previous closed trade")
        try:
            elapsed = (now_dt - _now(last)).total_seconds()
        except Exception:
            return audit.log("min_spacing", False, message=f"unreadable closed_at {last!r}")
        ok = elapsed >= r.min_seconds_between_trades
        return audit.log("min_spacing", ok, value=elapsed,
                         threshold=float(r.min_seconds_between_trades),
                         message=f"{elapsed:.0f}s since last closed trade < "
                         f"{r.min_seconds_between_trades}s" if not ok else "")

    # 12. model confidence ---------------------------------------------------------------
    def rule_confidence():
        conf = getattr(decision, "confidence_mean", None)
        if conf is None:
            return audit.log("min_confidence", False, message="no confidence from vote")
        ok = float(conf) >= r.min_confidence
        return audit.log("min_confidence", ok, value=float(conf),
                         threshold=float(r.min_confidence),
                         message=f"confidence {conf} < min {r.min_confidence}" if not ok else "")

    for rule in (rule_symbol, rule_action, rule_no_position, rule_max_positions,
                 rule_size, rule_thin_book, rule_sltp, rule_reward_risk,
                 rule_net_edge, rule_daily_loss, rule_spacing, rule_confidence):
        audit.safe(rule.__name__.removeprefix("rule_"), rule)

    return GuardResult(
        allowed=not audit.failures,
        reasons=list(audit.failures),
        stop_pct=stop,
        target_pct=target,
        size_tmn=size,
    )


def exit_allowed(db: Any, cfg: Any, symbol: str, reason_kind: str,
                 decision: Any = None, snapshot: Optional[Dict[str, Any]] = None) -> bool:
    """Exits are NEVER blocked (AGENTS.md hard rule 3): stop-loss, take-profit,
    shutdown and model exits always return True. The guard only logs the
    exemption to risk_checks for the audit trail."""
    snapshot = snapshot or {}
    try:
        db.insert_risk_check(
            run_id=snapshot.get("run_id"), decision_id=snapshot.get("decision_id"),
            rule=f"exit_{reason_kind}", passed=True, value=None, threshold=None,
            message="exits are exempt from entry-only vetoes",
        )
    except Exception:
        pass
    return True
