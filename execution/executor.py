"""Order execution (execution/executor.py).

The executor owns every order the agent places. It never raises out of a
public method: every outcome is an ExecutionResult and every anomaly is an
events row. One RLock per symbol (shared with main.py and the guardian via
symbol_lock()) so a decision cycle and a stop-loss close can never double-act
on the same symbol.

Entry (AGENTS.md): LIMIT BUY at the depth-aware price from book.entry_limit_price,
polled until entry_fill_timeout_sec; on timeout the order is cancelled and
re-fetched (race: it may have filled during the cancel); a partial fill keeps
what was filled, at the REAL average entry price.

Exit: MARKET SELL first; symbols whose MARKET is rejected with a 4xx (not 429)
fall back permanently (in memory) to a marketable LIMIT SELL at best bid minus
close_limit_ticks ticks, escalating every failed attempt. The close loop
NEVER gives up: capped backoff, balances re-read every attempt, CRITICAL
event after every 10 consecutive failures.

PnL is computed from real executed sums and fees, persisted through
db.close_position so the guard's 24h loss cap reads it.
"""
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional

from exchange import book
from exchange.wallex import WallexError, base_asset

CLIENT_ID_PREFIX = "ag_"
CLOSE_LOCK_TIMEOUT_SEC = 30.0
CRITICAL_AFTER_ATTEMPTS = 10
EPS = 1e-9

_PRINT_LOCK = threading.Lock()


def _console(message: str) -> None:
    """ASCII-safe, crash-proof console line (observability only)."""
    try:
        with _PRINT_LOCK:
            print(message.encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:
        pass


def _verbose(cfg: Any) -> int:
    try:
        return int(getattr(cfg.trading, "CONSOLE_VERBOSITY", 1))
    except Exception:
        return 1

# executor-internal close reasons -> store close_reason enum
REASON_TO_DB = {
    "SL": "STOP_LOSS",
    "TP": "TAKE_PROFIT",
    "MODEL_EXIT": "MODEL_SELL",
    "MANUAL": "MANUAL",
    "SHUTDOWN": "SHUTDOWN",
    "DUST": "DUST",
    "EXTERNAL": "EXTERNAL",
}


def _f(value: Any) -> float:
    """Tolerant float(value or 0)."""
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _floor_qty(qty: float, precision: Optional[int]) -> float:
    """Floor a quantity to the market's amount precision (Decimal, ROUND_DOWN)."""
    if qty is None or qty <= 0:
        return 0.0
    if precision is None:
        return float(qty)
    d = Decimal(str(qty)).quantize(Decimal(1).scaleb(-int(precision)), rounding=ROUND_DOWN)
    return float(d)


def _http_status(exc: BaseException) -> Optional[int]:
    return getattr(exc, "http_status", None)


def _is_rate_limit(exc: BaseException) -> bool:
    return _http_status(exc) == 429


def _is_transient(exc: BaseException) -> bool:
    """Timeouts, 5xx, 429 and network errors: retry, never flag the symbol."""
    if _is_rate_limit(exc):
        return True
    status = _http_status(exc)
    if status is not None:
        return status >= 500
    if isinstance(exc, TimeoutError):
        return True
    retryable = getattr(exc, "retryable", None)
    if retryable is True:
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "connection" in name


def _is_permanent_4xx(exc: BaseException) -> bool:
    status = _http_status(exc)
    return status is not None and 400 <= status < 500 and status != 429


def _is_price_required(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "price" in text and any(w in text for w in ("required", "must", "needed", "necessary"))


@dataclass(frozen=True)
class ExecutionResult:
    """Every public executor method returns this instead of raising."""
    ok: bool
    action: str                       # OPENED / CLOSED / HOLD / DUST / EXTERNAL / ERROR / CLOSING
    symbol: str
    reason: str
    order_id: Optional[int] = None
    position_id: Optional[int] = None
    client_id: Optional[str] = None
    entry_price: Optional[float] = None
    quantity: Optional[float] = None
    pnl_tmn: Optional[float] = None
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryContext:
    """Everything an entry needs; stop/target are already guard-selected."""
    decision_id: Optional[int]
    size_tmn: float
    stop_pct: float
    target_pct: float
    decision_uid: str = ""
    vote_summary: Dict[str, Any] = field(default_factory=dict)


class Executor:
    """Order execution for one exchange (paper or live), driven by config."""

    def __init__(self, cfg: Any, db: Any, exchange: Any,
                 clock: Any = time.time, sleep: Any = time.sleep,
                 run_id: Optional[int] = None):
        self.cfg = cfg
        self.db = db
        self.exchange = exchange
        self.clock = clock
        self.sleep = sleep
        self.run_id = run_id
        self._locks: Dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._run_token = uuid.uuid4().hex[:8]
        self._market_rejected: set = set()
        self._closing: set = set()
        self._warned_assets: set = set()
        # how long close paths block on a busy symbol lock (tests shrink it)
        self.close_lock_timeout = CLOSE_LOCK_TIMEOUT_SEC

    # ------------------------------------------------------------------ #
    # infra
    # ------------------------------------------------------------------ #
    def symbol_lock(self, symbol: str) -> threading.RLock:
        """Per-symbol lock shared with main.py and the guardian."""
        with self._locks_guard:
            lock = self._locks.get(symbol)
            if lock is None:
                lock = threading.RLock()
                self._locks[symbol] = lock
            return lock

    def is_closing(self, symbol: str) -> bool:
        """True while a close_position for this symbol is in progress."""
        return symbol in self._closing

    def _next_client_id(self, symbol: str) -> str:
        with self._counter_lock:
            self._counter += 1
            counter = self._counter
        # [A-Za-z0-9_]; uuid fragment makes restarts never collide
        return f"{CLIENT_ID_PREFIX}{int(self.clock())}_{symbol}_{counter}_{uuid.uuid4().hex[:6]}"

    def _event(self, level: str, kind: str, message: str,
               data: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.db.log_event(level, kind, message, data, run_id=self.run_id)
        except Exception:
            pass  # logging must never crash the bot (AGENTS.md rule 9)

    def _trace(self, exc: BaseException) -> str:
        try:
            return traceback.format_exc(limit=8)
        except Exception:
            return f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _hold(symbol: str, reason: str, detail: Optional[Dict[str, Any]] = None) -> ExecutionResult:
        return ExecutionResult(ok=False, action="HOLD", symbol=symbol,
                               reason=reason, detail=detail or {})

    # ------------------------------------------------------------------ #
    # order-state helpers
    # ------------------------------------------------------------------ #
    def _get_order_safe(self, client_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self.exchange.get_order(client_id)
            order = (resp or {}).get("order")
            return dict(order) if isinstance(order, dict) else None
        except Exception as exc:
            self._event("WARN", "GET_ORDER_ERROR",
                        f"get_order failed for {client_id}: {exc}")
            return None

    def _poll_order(self, client_id: str, timeout_sec: float) -> Optional[Dict[str, Any]]:
        """Poll until the order is finished (active False) or the timeout hits.
        Fully filled == active is False and executedQty > 0 -- NEVER status strings."""
        deadline = self.clock() + float(timeout_sec)
        order: Optional[Dict[str, Any]] = None
        while True:
            order = self._get_order_safe(client_id)
            if order is not None and order.get("active") is False:
                return order
            if self.clock() >= deadline:
                return order
            self.sleep(float(self.cfg.trading.order_poll_interval_sec))

    def _cancel_with_race(self, client_id: str, symbol: str) -> Optional[Dict[str, Any]]:
        """Cancel; ALWAYS re-fetch afterwards (it may have filled during the
        cancel). Retry the cancel with backoff while it stays active; never
        assume it was cancelled."""
        backoff = float(self.cfg.trading.close_retry_backoff_sec)
        cap = float(self.cfg.trading.close_retry_backoff_cap_sec)
        attempt = 0
        last_known: Optional[Dict[str, Any]] = None
        while True:
            attempt += 1
            cancel_error: Optional[BaseException] = None
            try:
                self.exchange.cancel_order(client_id)
            except Exception as exc:
                cancel_error = exc
            order = self._get_order_safe(client_id)
            if order is not None:
                last_known = order
                if order.get("active") is False:
                    return order
            elif cancel_error is None and attempt >= 3:
                # cancel accepted but status unreadable: stop hammering the API
                self._event("ERROR", "CRITICAL",
                            f"cancel of {client_id} accepted but status unreadable",
                            {"symbol": symbol, "client_id": client_id})
                return last_known
            if attempt >= CRITICAL_AFTER_ATTEMPTS and attempt % CRITICAL_AFTER_ATTEMPTS == 0:
                self._event("ERROR", "CRITICAL",
                            f"cancel of {client_id} still unconfirmed after {attempt} attempts",
                            {"symbol": symbol, "client_id": client_id})
            self.sleep(backoff)
            backoff = min(backoff * 2.0, cap)

    def _persist_order(self, *, client_id: str, symbol: str, side: str,
                       order_type: str, price: Optional[float], quantity: float,
                       order: Dict[str, Any], decision_id: Optional[int],
                       request_json: Dict[str, Any]) -> Optional[int]:
        # PaperExchange already persists its own order row with this client_id;
        # reuse it (and attach our decision_id) instead of colliding on the
        # UNIQUE constraint. In live mode no row exists yet and we insert one.
        existing = None
        try:
            existing = self.db.get_order_by_client_id(client_id)
        except Exception:
            existing = None
        if existing is not None:
            try:
                self.db.update_order(
                    existing["id"], decision_id=decision_id,
                    status=order.get("status"),
                    filled_quantity=_f(order.get("executedQty")) or None,
                    avg_fill_price=_f(order.get("executedPrice")) or None,
                    fee_tmn=_f(order.get("fee")) or None)
            except Exception:
                pass
            return existing["id"]
        try:
            row_id = self.db.insert_order(
                run_id=self.run_id, decision_id=decision_id, client_id=client_id,
                symbol=symbol, side=side, type=order_type, price=price,
                quantity=quantity, status=order.get("status"),
                filled_quantity=_f(order.get("executedQty")) or None,
                avg_fill_price=_f(order.get("executedPrice")) or None,
                fee_tmn=_f(order.get("fee")) or None,
                dry_run=bool(self.cfg.trading.dry_run),
                request_json=request_json, response_json=order,
            )
        except Exception:
            return None
        if row_id is None:
            self._event("ERROR", "ORDER_ROW_FAILED",
                        f"failed to persist order row for {client_id}", {"symbol": symbol})
        return row_id

    def _best_bid(self, symbol: str) -> Optional[float]:
        try:
            depth = self.exchange.get_depth(symbol)
            bids = depth.get("bids") or []
            return float(bids[0][0]) if bids else None
        except Exception as exc:
            self._event("WARN", "DEPTH_ERROR", f"depth read failed for {symbol}: {exc}")
            return None

    def _precisions(self, symbol: str) -> tuple:
        try:
            market = self.exchange.get_market_info(symbol)
        except Exception as exc:
            self._event("WARN", "MARKET_INFO_ERROR",
                        f"market info read failed for {symbol}: {exc}")
            return None, None
        if market is None:
            return None, None
        return market.price_precision, market.amount_precision

    # ------------------------------------------------------------------ #
    # 1.1 open_position
    # ------------------------------------------------------------------ #
    def open_position(self, symbol: str, ctx: EntryContext) -> ExecutionResult:
        lock = self.symbol_lock(symbol)
        lock.acquire()  # reentrant for the decision thread
        try:
            return self._open_locked(symbol, ctx)
        except Exception as exc:
            self._event("ERROR", "EXECUTOR_CRASH",
                        f"open_position crashed for {symbol}: {exc}",
                        {"symbol": symbol, "traceback": self._trace(exc)})
            return ExecutionResult(ok=False, action="ERROR", symbol=symbol,
                                   reason=f"crash: {exc}")
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass

    def _open_locked(self, symbol: str, ctx: EntryContext) -> ExecutionResult:
        t = self.cfg.trading
        # a) fresh book + depth-aware limit price
        try:
            depth = self.exchange.get_depth(symbol)
        except Exception as exc:
            status = _http_status(exc)
            detail: Dict[str, Any] = {"error": str(exc)}
            if status is not None:
                detail["http_status"] = status
            self._event("WARN", "ENTRY_DATA_ERROR",
                        f"depth read failed for {symbol}: {exc}", detail)
            return self._hold(symbol, "data_error", detail)
        price_precision, amount_precision = self._precisions(symbol)
        asks = depth.get("asks") or []
        limit_price = book.entry_limit_price(
            asks, ctx.size_tmn, t.entry_max_slippage_pct,
            t.entry_limit_ticks_above, price_precision)
        if limit_price is None:
            self._event("INFO", "ENTRY_REJECTED",
                        f"thin book on {symbol}: cannot cover {ctx.size_tmn:.0f} TMN",
                        {"symbol": symbol, "size_tmn": ctx.size_tmn})
            return self._hold(symbol, "thin_book", {"size_tmn": ctx.size_tmn})

        # b) quantity, floored to the market precision
        qty = _floor_qty(ctx.size_tmn / limit_price, amount_precision)
        notional = qty * limit_price
        if qty <= 0 or notional < self.cfg.risk.min_order_tmn:
            reason = "qty_zero" if qty <= 0 else "size_below_min"
            self._event("INFO", "ENTRY_REJECTED",
                        f"sizing rejected on {symbol}: qty={qty}, notional={notional:.0f}",
                        {"symbol": symbol, "qty": qty, "notional": notional})
            return self._hold(symbol, reason, {"qty": qty, "notional": notional})

        # c)+d) place LIMIT BUY and persist BEFORE polling
        client_id = self._next_client_id(symbol)
        try:
            resp = self.exchange.create_order(symbol, "BUY", "LIMIT",
                                              quantity=qty, price=limit_price,
                                              client_id=client_id)
        except Exception as exc:
            detail = {"error": str(exc)}
            status = _http_status(exc)
            if status is not None:
                detail["http_status"] = status
            self._event("WARN", "ENTRY_REJECTED",
                        f"create LIMIT BUY failed on {symbol}: {exc}", detail)
            return self._hold(symbol, "order_rejected", detail)
        created = (resp or {}).get("order") or {}
        if _verbose(self.cfg) >= 1:
            _console(f"[ENTRY] {symbol} LIMIT BUY {qty:.8g} @ {limit_price:.8g} "
                     f"placed ({client_id})")
        order_row_id = self._persist_order(
            client_id=client_id, symbol=symbol, side="BUY", order_type="LIMIT",
            price=limit_price, quantity=qty, order=created,
            decision_id=ctx.decision_id,
            request_json={"side": "BUY", "type": "LIMIT", "price": limit_price,
                          "quantity": qty, "client_id": client_id})
        if order_row_id is None:
            self._event("ERROR", "CRITICAL",
                        f"order {client_id} is live but its DB row failed to persist; "
                        "still tracking it", {"symbol": symbol, "client_id": client_id})

        # e) poll until filled or entry_fill_timeout_sec
        final = self._poll_order(client_id, t.entry_fill_timeout_sec)
        # f) timeout: cancel, then ALWAYS re-fetch (race)
        if final is None or final.get("active") is not False:
            final = self._cancel_with_race(client_id, symbol) or final or {}

        executed = _f(final.get("executedQty"))
        if executed <= 0:
            if order_row_id is not None:
                try:
                    self.db.update_order(order_row_id,
                                         status=final.get("status") or "CANCELED")
                except Exception:
                    pass
            self._event("INFO", "ENTRY_UNFILLED",
                        f"entry order {client_id} on {symbol} did not fill; cancelled",
                        {"symbol": symbol, "client_id": client_id,
                         "decision_uid": ctx.decision_uid})
            if _verbose(self.cfg) >= 1:
                _console(f"[ENTRY] {symbol} unfilled, cancelled ({client_id})")
            return self._hold(symbol, "entry_unfilled",
                               {"client_id": client_id, "order_id": order_row_id})

        # g) REAL average entry price
        entry_price = _f(final.get("executedPrice"))
        if entry_price <= 0:
            executed_sum = _f(final.get("executedSum"))
            entry_price = (executed_sum / executed) if executed_sum > 0 else limit_price

        # h) persist the open position; SL/TP from guard-selected pcts vs the real price
        stop_price = entry_price * (1.0 - ctx.stop_pct)
        target_price = entry_price * (1.0 + ctx.target_pct)
        position_id = None
        try:
            position_id = self.db.insert_position(
                run_id=self.run_id, symbol=symbol, entry_price=entry_price,
                quantity=executed, entry_value_tmn=executed * entry_price,
                stop_pct=ctx.stop_pct, target_pct=ctx.target_pct,
                stop_price=stop_price, target_price=target_price,
                dry_run=bool(t.dry_run), entry_decision_id=ctx.decision_id,
                entry_order_id=order_row_id)
        except Exception:
            position_id = None
        if position_id is None:
            self._event("ERROR", "CRITICAL",
                        f"filled order {client_id} on {symbol} but the position row "
                        "failed to persist", {"symbol": symbol, "client_id": client_id})
            return ExecutionResult(ok=False, action="ERROR", symbol=symbol,
                                   reason="position_row_failed",
                                   order_id=order_row_id, client_id=client_id,
                                   entry_price=entry_price, quantity=executed)
        entry_fee = _f(final.get("fee"))
        try:
            if entry_fee:
                self.db.update_position(position_id, fees_tmn=entry_fee)
            if order_row_id is not None:
                self.db.update_order(order_row_id, position_id=position_id,
                                     filled_quantity=executed,
                                     avg_fill_price=entry_price or None,
                                     status=final.get("status"))
        except Exception:
            pass

        partial = executed < qty - EPS
        if _verbose(self.cfg) >= 1:
            _console(f"[ENTRY] {symbol} {'partially ' if partial else ''}filled "
                     f"{executed:.8g} @ {entry_price:.8g}")
        self._event("INFO", "ENTRY_FILLED",
                    f"{'partial ' if partial else ''}entry on {symbol}: "
                    f"{executed} @ {entry_price}",
                    {"symbol": symbol, "client_id": client_id,
                     "entry_price": entry_price, "qty": executed,
                     "decision_uid": ctx.decision_uid})
        return ExecutionResult(
            ok=True, action="OPENED", symbol=symbol,
            reason="partial_fill" if partial else "filled",
            order_id=order_row_id, position_id=position_id,
            client_id=client_id, entry_price=entry_price, quantity=executed,
            detail={"stop_price": stop_price, "target_price": target_price,
                    "limit_price": limit_price})

    # ------------------------------------------------------------------ #
    # 1.2 close_position
    # ------------------------------------------------------------------ #
    def close_position(self, symbol: str, reason: str,
                       decision_id: Optional[int] = None) -> ExecutionResult:
        lock = self.symbol_lock(symbol)
        try:
            acquired = lock.acquire(timeout=self.close_lock_timeout)
        except TypeError:  # very old Pythons: acquire() has no timeout
            acquired = lock.acquire()
        if not acquired:
            return ExecutionResult(ok=False, action="ERROR", symbol=symbol,
                                    reason="lock_timeout")
        try:
            pos = self._open_position_row(symbol)
            if pos is None:  # idempotent
                return ExecutionResult(ok=True, action="CLOSED", symbol=symbol,
                                       reason="no_open_position")
            if symbol in self._closing:  # another thread is already closing it
                return ExecutionResult(ok=True, action="CLOSING", symbol=symbol,
                                       reason="already_closing")
            self._closing.add(symbol)
            try:
                return self._close_locked(symbol, pos, reason, decision_id)
            finally:
                self._closing.discard(symbol)
        except Exception as exc:
            self._event("ERROR", "EXECUTOR_CRASH",
                        f"close_position crashed for {symbol}: {exc}",
                        {"symbol": symbol, "traceback": self._trace(exc)})
            return ExecutionResult(ok=False, action="ERROR", symbol=symbol,
                                    reason=f"crash: {exc}")
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass

    def _open_position_row(self, symbol: str) -> Optional[Dict[str, Any]]:
        positions = self.db.get_open_positions(bool(self.cfg.trading.dry_run))
        for pos in positions:
            if pos.get("symbol") == symbol:
                return pos
        return None

    def _close_locked(self, symbol: str, pos: Dict[str, Any], reason: str,
                      decision_id: Optional[int]) -> ExecutionResult:
        t = self.cfg.trading
        base = base_asset(symbol)
        price_precision, amount_precision = self._precisions(symbol)
        tick = 10.0 ** (-price_precision) if price_precision else 1.0
        pos_qty = _f(pos.get("quantity"))
        entry_value = _f(pos.get("entry_value_tmn"))
        entry_fees = _f(pos.get("fees_tmn"))
        db_reason = REASON_TO_DB.get(reason, reason)

        sold_qty = 0.0
        proceeds = 0.0
        close_fees = 0.0
        exit_order_id: Optional[int] = None
        escalate_ticks = 0
        allow_market_price_retry = True
        attempt = 0
        backoff = float(t.close_retry_backoff_sec)
        backoff_cap = float(t.close_retry_backoff_cap_sec)

        while True:
            attempt += 1
            remaining = pos_qty - sold_qty
            if remaining <= EPS:
                break  # done
            # re-read the balances every attempt
            try:
                balances = self.exchange.get_balances()
            except Exception as exc:
                self._event("WARN", "CLOSE_ATTEMPT",
                            f"balance read failed during close of {symbol}: {exc}",
                            {"symbol": symbol, "attempt": attempt, "failed": True,
                             "error": str(exc)})
                self.sleep(backoff)
                backoff = min(backoff * 2.0, backoff_cap)
                continue
            free = _f((balances.get(base) or {}).get("free"))
            locked = _f((balances.get(base) or {}).get("locked"))

            # free balance gone while we still think we hold the position
            if free <= EPS:
                if sold_qty > EPS:
                    break  # sold what we could; the rest is dust
                if locked > EPS:
                    # coins locked behind (probably our own) resting order:
                    # wait for it to resolve; never force-close as dust/external
                    self._event("INFO", "CLOSE_ATTEMPT",
                                f"coins for {symbol} are locked; waiting",
                                {"symbol": symbol, "attempt": attempt,
                                 "locked": locked})
                    if attempt >= CRITICAL_AFTER_ATTEMPTS and \
                            attempt % CRITICAL_AFTER_ATTEMPTS == 0:
                        self._event("ERROR", "CRITICAL",
                                    f"{attempt} close attempts on {symbol} "
                                    "(coins locked); still waiting",
                                    {"symbol": symbol, "attempt": attempt})
                    self.sleep(backoff)
                    backoff = min(backoff * 2.0, backoff_cap)
                    continue
                if free + locked > 1e-12:
                    # a sub-precision crumb is left after fees -> dust
                    bid = self._best_bid(symbol) or 0.0
                    dust_qty = min(remaining, free + locked)
                    dust_value = dust_qty * bid
                    return self._finish_close(
                        pos, "DUST", symbol, bid, dust_value, entry_fees,
                        dust_value - entry_value - entry_fees, exit_order_id)
                # coins vanished with no sell of ours -> external close
                bid = self._best_bid(symbol)
                mark_proceeds = remaining * (bid or 0.0)
                return self._finish_close(
                    pos, "EXTERNAL", symbol, bid or 0.0,
                    mark_proceeds, entry_fees,
                    mark_proceeds - entry_value - entry_fees,
                    exit_order_id, estimated=True)

            # sell quantity: never more than the free balance, never more than
            # the position (pre-existing coins are never sold)
            qty = _floor_qty(min(free, remaining), amount_precision)
            if qty <= 0:
                if sold_qty > EPS:
                    break  # remainder floors to nothing: dust, keep the sale
                # sub-precision dust left after fees
                bid = self._best_bid(symbol) or 0.0
                dust_qty = min(remaining, free)
                dust_value = dust_qty * bid
                return self._finish_close(
                    pos, "DUST", symbol, bid, dust_value, entry_fees,
                    dust_value - entry_value - entry_fees, exit_order_id)

            # place one closing order (MARKET first, marketable LIMIT fallback)
            if _verbose(self.cfg) >= 1:
                _console(f"[CLOSE] {symbol} attempt {attempt}: sell {qty:.8g} "
                         f"(limit escalation +{escalate_ticks} ticks)")
            self._event("INFO", "CLOSE_ATTEMPT",
                        f"close attempt {attempt} for {symbol}: selling {qty}",
                        {"symbol": symbol, "attempt": attempt, "qty": qty,
                         "reason": reason, "escalate_ticks": escalate_ticks})
            try:
                res = self._place_close_order(
                    symbol, qty, escalate_ticks, decision_id,
                    allow_market_price_retry)
                if res.get("market_price_retry_used"):
                    allow_market_price_retry = False
            except Exception as exc:
                self._event("WARN", "CLOSE_ATTEMPT",
                            f"close attempt {attempt} failed for {symbol}: {exc}",
                            {"symbol": symbol, "attempt": attempt, "failed": True,
                             "error": str(exc), "transient": _is_transient(exc),
                             "rate_limited": _is_rate_limit(exc)})
                if _verbose(self.cfg) >= 1:
                    _console(f"[CLOSE] {symbol} attempt {attempt} failed: {exc}")
                if attempt >= CRITICAL_AFTER_ATTEMPTS and \
                        attempt % CRITICAL_AFTER_ATTEMPTS == 0:
                    self._event("ERROR", "CRITICAL",
                                f"{attempt} consecutive close failures on {symbol}; "
                                "still retrying", {"symbol": symbol, "attempt": attempt})
                self.sleep(backoff)
                backoff = min(backoff * 2.0, backoff_cap)
                continue

            filled = _f(res.get("filled_qty"))
            sold_qty += filled
            proceeds += filled * _f(res.get("avg_price"))
            close_fees += _f(res.get("fee"))
            exit_order_id = res.get("order_row_id") or exit_order_id
            if _verbose(self.cfg) >= 1:
                _console(f"[CLOSE] {symbol} attempt {attempt}: filled "
                         f"{filled:.8g} @ {_f(res.get('avg_price')):.8g}")
            if filled < qty - EPS:
                # unfilled remainder: escalate the limit price for the retry
                escalate_ticks += int(t.close_limit_ticks)
            if pos_qty - sold_qty <= EPS:
                break  # fully sold: no extra backoff before finishing
            if attempt >= CRITICAL_AFTER_ATTEMPTS and \
                    attempt % CRITICAL_AFTER_ATTEMPTS == 0:
                self._event("ERROR", "CRITICAL",
                            f"{attempt} close attempts on {symbol}; still retrying",
                            {"symbol": symbol, "attempt": attempt,
                             "sold_qty": sold_qty})
            self.sleep(backoff)
            backoff = min(backoff * 2.0, backoff_cap)

        avg_close = (proceeds / sold_qty) if sold_qty > EPS else 0.0
        fees_total = entry_fees + close_fees
        return self._finish_close(pos, db_reason, symbol, avg_close,
                                  proceeds, fees_total,
                                  proceeds - entry_value - fees_total,
                                  exit_order_id)

    def _place_close_order(self, symbol: str, qty: float, escalate_ticks: int,
                           decision_id: Optional[int],
                           allow_market_price_retry: bool) -> Dict[str, Any]:
        """One closing order: MARKET first; marketable LIMIT fallback.

        Raises on placement failure (caller logs the attempt and retries);
        returns fill info once the order reached a terminal state.
        """
        t = self.cfg.trading
        price_precision, amount_precision = self._precisions(symbol)
        tick = 10.0 ** (-price_precision) if price_precision else 1.0
        try:
            depth = self.exchange.get_depth(symbol)
        except Exception as exc:
            raise exc
        bids = depth.get("bids") or []
        if not bids:
            raise RuntimeError("empty bid book")
        best_bid = float(bids[0][0])

        placed = False
        used_market_price_retry = False
        order_type = "LIMIT"
        price: Optional[float] = None
        client_id = self._next_client_id(symbol)
        request_json: Dict[str, Any] = {}

        if symbol not in self._market_rejected:
            try:
                request_json = {"side": "SELL", "type": "MARKET",
                                "quantity": qty, "client_id": client_id}
                resp = self.exchange.create_order(symbol, "SELL", "MARKET",
                                                  quantity=qty, client_id=client_id)
                placed = True
                order_type = "MARKET"
            except Exception as exc:
                if _is_rate_limit(exc) or _is_transient(exc):
                    raise  # no flag, outer loop backs off
                if _is_price_required(exc) and allow_market_price_retry:
                    # ONE retry of MARKET with price = best bid
                    client_id = self._next_client_id(symbol)
                    request_json = {"side": "SELL", "type": "MARKET",
                                    "quantity": qty, "price": best_bid,
                                    "client_id": client_id}
                    resp = self.exchange.create_order(symbol, "SELL", "MARKET",
                                                      quantity=qty, price=best_bid,
                                                      client_id=client_id)
                    placed = True
                    order_type = "MARKET"
                    used_market_price_retry = True
                else:
                    if _is_permanent_4xx(exc):
                        self._market_rejected.add(symbol)
                        self._event("WARN", "MARKET_REJECTED",
                                    f"MARKET sell rejected on {symbol}; "
                                    "using marketable LIMIT from now on",
                                    {"symbol": symbol, "error": str(exc)})
                    # fall through to the LIMIT fallback below

        if not placed:
            limit = best_bid - (int(t.close_limit_ticks) + escalate_ticks) * tick
            client_id = self._next_client_id(symbol)
            request_json = {"side": "SELL", "type": "LIMIT", "price": limit,
                            "quantity": qty, "client_id": client_id}
            resp = self.exchange.create_order(symbol, "SELL", "LIMIT",
                                              quantity=qty, price=limit,
                                              client_id=client_id)
            order_type = "LIMIT"
            price = limit

        created = (resp or {}).get("order") or {}
        order_row_id = self._persist_order(
            client_id=client_id, symbol=symbol, side="SELL", order_type=order_type,
            price=price, quantity=qty, order=created, decision_id=decision_id,
            request_json=request_json)
        if order_row_id is None:
            self._event("ERROR", "CRITICAL",
                        f"close order {client_id} is live but its DB row failed",
                        {"symbol": symbol, "client_id": client_id})

        final = self._poll_order(client_id, t.close_fill_timeout_sec)
        if final is None or final.get("active") is not False:
            final = self._cancel_with_race(client_id, symbol) or final or {}
        filled = _f(final.get("executedQty"))
        avg = _f(final.get("executedPrice"))
        if avg <= 0:
            executed_sum = _f(final.get("executedSum"))
            avg = (executed_sum / filled) if (executed_sum > 0 and filled > 0) else \
                (price if price is not None else best_bid)
        if order_row_id is not None:
            try:
                self.db.update_order(order_row_id, status=final.get("status"),
                                     filled_quantity=filled or None,
                                     avg_fill_price=avg or None)
            except Exception:
                pass
        return {"filled_qty": filled, "avg_price": avg, "fee": _f(final.get("fee")),
                "order_row_id": order_row_id, "client_id": client_id,
                "market_price_retry_used": used_market_price_retry}

    def _finish_close(self, pos: Dict[str, Any], db_reason: str, symbol: str,
                      close_price: float, proceeds: float, fees_total: float,
                      pnl: float, exit_order_id: Optional[int],
                      estimated: bool = False) -> ExecutionResult:
        """Persist the close (retrying the DB write), emit the event, build the result."""
        ok = False
        for attempt in range(1, 6):
            try:
                ok = self.db.close_position(
                    pos["id"], close_price, db_reason, pnl, fees_total,
                    exit_order_id=exit_order_id)
            except Exception:
                ok = False
            if ok:
                break
            self._event("ERROR", "CLOSE_ROW_FAILED",
                        f"failed to persist close of position {pos.get('id')} "
                        f"(attempt {attempt})", {"symbol": symbol})
            self.sleep(float(self.cfg.trading.close_retry_backoff_sec))
        if not ok:
            self._event("ERROR", "CRITICAL",
                        f"position {pos.get('id')} on {symbol} closed on the "
                        "exchange but the DB row would not update",
                        {"symbol": symbol, "pnl_tmn": pnl, "reason": db_reason})
        self._event("INFO", "POSITION_CLOSED",
                    f"closed {symbol} ({db_reason}): pnl {pnl:.0f} TMN",
                    {"symbol": symbol, "reason": db_reason, "pnl_tmn": pnl,
                     "proceeds": proceeds, "fees": fees_total,
                     "estimated": estimated, "position_id": pos.get("id")})
        return ExecutionResult(ok=ok, action="CLOSED", symbol=symbol,
                                reason=db_reason, position_id=pos.get("id"),
                                order_id=exit_order_id, pnl_tmn=pnl,
                                detail={"proceeds": proceeds, "fees": fees_total,
                                        "estimated": estimated})

    # ------------------------------------------------------------------ #
    # 1.3 close_all
    # ------------------------------------------------------------------ #
    def close_all(self, reason: str = "SHUTDOWN") -> Dict[str, Any]:
        results: List[ExecutionResult] = []
        try:
            positions = self.db.get_open_positions(bool(self.cfg.trading.dry_run))
        except Exception as exc:
            self._event("ERROR", "EXECUTOR_CRASH",
                        f"close_all could not read positions: {exc}")
            return {"ok": False, "results": results, "remaining": None}
        for pos in positions:
            res = self.close_position(pos.get("symbol"), reason)
            results.append(res)
        remaining = len(self._open_symbols())
        return {"ok": remaining == 0, "results": results, "remaining": remaining}

    def _open_symbols(self) -> List[str]:
        try:
            positions = self.db.get_open_positions(bool(self.cfg.trading.dry_run))
        except Exception:
            return []
        return sorted({p.get("symbol") for p in positions})

    def open_symbols(self) -> List[str]:
        """Public: symbols that currently have an OPEN position in the DB."""
        return self._open_symbols()

    # ------------------------------------------------------------------ #
    # 1.4 sync_with_exchange
    # ------------------------------------------------------------------ #
    def sync_with_exchange(self) -> Dict[str, Any]:
        summary: Dict[str, Any] = {"external_closes": 0, "balance_warnings": 0,
                                  "orphan_cancels": 0, "foreign_untouched": 0,
                                  "market_flag_reset": len(self._market_rejected)}
        try:
            return self._sync_locked(summary)
        except Exception as exc:
            self._event("ERROR", "EXECUTOR_CRASH",
                        f"sync_with_exchange crashed: {exc}",
                        {"traceback": self._trace(exc)})
            return summary

    def _sync_locked(self, summary: Dict[str, Any]) -> Dict[str, Any]:
        dry = bool(self.cfg.trading.dry_run)
        try:
            positions = self.db.get_open_positions(dry)
        except Exception as exc:
            self._event("ERROR", "SYNC_ERROR", f"cannot read positions: {exc}")
            return summary
        try:
            balances = self.exchange.get_balances()
        except Exception as exc:
            self._event("WARN", "SYNC_ERROR", f"cannot read balances: {exc}")
            balances = None

        try:
            open_orders = self.exchange.get_open_orders()
            open_orders = open_orders.get("orders") or []
        except Exception as exc:
            self._event("WARN", "SYNC_ERROR", f"cannot read open orders: {exc}")
            open_orders = []

        # (a) DB-open position whose coins are gone -> EXTERNAL close
        if balances is not None:
            for pos in positions:
                symbol = pos.get("symbol")
                base = base_asset(symbol)
                info = balances.get(base) or {}
                total = _f(info.get("free")) + _f(info.get("locked"))
                if total > EPS:
                    continue
                our_sell_open = any(
                    o.get("side") == "SELL" and o.get("symbol") == symbol and
                    str(o.get("clientOrderId") or "").startswith(CLIENT_ID_PREFIX)
                    for o in open_orders)
                if our_sell_open:
                    continue  # our closing order is still working
                bid = self._best_bid(symbol) or 0.0
                remaining = _f(pos.get("quantity"))
                mark = remaining * bid
                pnl = mark - _f(pos.get("entry_value_tmn")) - _f(pos.get("fees_tmn"))
                pos2 = dict(pos)
                res = self._finish_close(pos2, "EXTERNAL", symbol, bid, mark,
                                         _f(pos.get("fees_tmn")), pnl, None,
                                         estimated=True)
                if res.ok:
                    summary["external_closes"] += 1

        # (b) configured-asset balance without a DB position -> warning only
        position_symbols = {p.get("symbol") for p in positions}
        if balances is not None:
            for symbol in self.cfg.trading.SYMBOLS:
                if symbol in position_symbols:
                    continue
                base = base_asset(symbol)
                info = balances.get(base) or {}
                total = _f(info.get("free")) + _f(info.get("locked"))
                if total > EPS:
                    summary["balance_warnings"] += 1
                    self._event("WARN", "UNTRACKED_BALANCE",
                                f"{base} balance {total} on {symbol} with no open "
                                "position; never traded, never sold",
                                {"symbol": symbol, "asset": base, "amount": total})

        # (c) orphan open orders carrying OUR prefix -> cancel; foreign untouched
        for order in open_orders:
            client_id = str(order.get("clientOrderId") or "")
            if not client_id.startswith(CLIENT_ID_PREFIX):
                summary["foreign_untouched"] += 1
                continue
            row = None
            try:
                row = self.db.get_order_by_client_id(client_id)
            except Exception:
                row = None
            stale = False
            if row is not None:
                status = str(row.get("status") or "").upper()
                stale = status in ("FILLED", "CANCELED", "CANCELED", "REJECTED")
            if row is None or stale:
                symbol = order.get("symbol")
                lock = self.symbol_lock(symbol)
                try:
                    lock.acquire(timeout=CLOSE_LOCK_TIMEOUT_SEC)
                except TypeError:
                    lock.acquire()
                try:
                    self.exchange.cancel_order(client_id)
                    summary["orphan_cancels"] += 1
                    self._event("INFO", "ORPHAN_CANCELLED",
                                f"cancelled orphan order {client_id} on {symbol}",
                                {"symbol": symbol, "client_id": client_id})
                except Exception as exc:
                    self._event("WARN", "ORPHAN_CANCEL_FAILED",
                                f"could not cancel orphan {client_id}: {exc}",
                                {"symbol": symbol, "client_id": client_id})
                finally:
                    try:
                        lock.release()
                    except RuntimeError:
                        pass

        # (d) in-memory state rebuilt from nothing
        self._market_rejected.clear()
        return summary

    # ------------------------------------------------------------------ #
    # 1.5 snapshot (guard account contract)
    # ------------------------------------------------------------------ #
    def snapshot(self) -> Dict[str, Any]:
        """Account valuation exactly as risk.guard.check_entry expects:
        total_value_tmn = free+locked TMN + holdings of CONFIG.SYMBOLS bases at
        best bid; free_tmn = spendable TMN. Unknown assets -> warning only."""
        try:
            balances = self.exchange.get_balances()
        except Exception as exc:
            self._event("WARN", "SNAPSHOT_ERROR", f"balance read failed: {exc}")
            return {"total_value_tmn": None, "free_tmn": None,
                    "tmn_locked": None, "assets": {}, "error": str(exc)}
        tmn = balances.get("TMN") or {}
        free_tmn = _f(tmn.get("free"))
        locked_tmn = _f(tmn.get("locked"))
        total = free_tmn + locked_tmn
        assets: Dict[str, Any] = {"TMN": {"free": free_tmn, "locked": locked_tmn}}
        for symbol in self.cfg.trading.SYMBOLS:
            base = base_asset(symbol)
            info = balances.get(base) or {}
            amount = _f(info.get("free")) + _f(info.get("locked"))
            if amount <= EPS:
                continue
            bid = self._best_bid(symbol)
            if bid is None:
                continue
            assets[base] = {"amount": amount, "best_bid": bid,
                            "value_tmn": amount * bid}
            total += amount * bid
        known = {"TMN"} | {base_asset(s) for s in self.cfg.trading.SYMBOLS}
        for asset, info in balances.items():
            amount = _f((info or {}).get("free")) + _f((info or {}).get("locked"))
            if asset not in known and amount > EPS and asset not in self._warned_assets:
                self._warned_assets.add(asset)
                self._event("WARN", "UNVALUED_ASSET",
                            f"asset {asset} ({amount}) is not a configured market "
                            "base; excluded from account value",
                            {"asset": asset, "amount": amount})
        return {"total_value_tmn": total, "free_tmn": free_tmn,
                "tmn_locked": locked_tmn, "assets": assets}
