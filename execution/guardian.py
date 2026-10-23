"""Guardian thread (execution/guardian.py).

Watches open positions and closes them on stop-loss / take-profit by reading
the best bid -- the executable price for a long position (AGENTS.md hard
rule 4). It calls the executor directly: no risk checks, no model votes, no
daily-loss cap, no cooldown ever stand between a position and its exit.

Import rule: this module imports NOTHING from the decision stack (no risk
guard, no model/prompt/vote modules, no application entry module). The
out-of-cycle decision callback is injected as `on_trigger`.

The thread must never die: every per-symbol block is exception-guarded, a
429 only backs the loop off for one tick, and the supervisor restarts the
loop and logs it if it somehow exits.
"""
import collections
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

EPS = 1e-9

_PRINT_LOCK = threading.Lock()


def _console(message: str) -> None:
    """ASCII-safe, crash-proof console line (observability only)."""
    try:
        with _PRINT_LOCK:
            print(message.encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:
        pass


def format_watch_line(data: Dict[str, Any]) -> str:
    """Pure formatter for the per-symbol guardian watch line (unit-tested).

    data keys: symbol, bid, has_position, and for flat symbols move_pct,
    threshold_pct, move_frac, cooldown_remaining; for positions entry,
    sl_price, tp_price, dist_sl_pct, dist_tp_pct, ok.
    """
    symbol = data.get("symbol", "?")
    bid = data.get("bid")
    bid_s = f"{bid:.12g}" if isinstance(bid, (int, float)) else "?"
    if data.get("has_position"):
        entry = data.get("entry")
        sl = data.get("sl_price")
        tp = data.get("tp_price")
        dsl = data.get("dist_sl_pct")
        dtp = data.get("dist_tp_pct")

        def _g(v, digits=2):
            return f"{v:+.{digits}f}%" if isinstance(v, (int, float)) else "?"

        entry_s = f"{entry:.12g}" if isinstance(entry, (int, float)) else "?"
        sl_s = f"{sl:.12g}" if isinstance(sl, (int, float)) else "?"
        tp_s = f"{tp:.12g}" if isinstance(tp, (int, float)) else "?"
        state = "OK" if data.get("ok", True) else "HIT"
        return (f"[GUARDIAN] {symbol} bid={bid_s} position entry={entry_s} "
                f"SL={sl_s} ({_g(dsl)}) TP={tp_s} ({_g(dtp)}) {state}")
    move = data.get("move_pct")
    move_s = f"{move:+.2f}%" if isinstance(move, (int, float)) else "?"
    thr = data.get("threshold_pct")
    thr_s = f"{thr:.2f}%" if isinstance(thr, (int, float)) else "?"
    frac = data.get("move_frac")
    frac_s = f" ({frac * 100:.0f}%)" if isinstance(frac, (int, float)) else ""
    cooldown = data.get("cooldown_remaining")
    cd_s = f"{cooldown:.0f}s" if isinstance(cooldown, (int, float)) else "?"
    return (f"[GUARDIAN] {symbol} bid={bid_s} move(window)={move_s} / "
            f"trigger {thr_s}{frac_s} cooldown={cd_s} no-position")


def watch_event_data(data: Dict[str, Any], t: Any) -> Dict[str, Any]:
    """Pure builder for the GUARDIAN_WATCH event payload (unit-tested)."""
    payload: Dict[str, Any] = {
        "symbol": data.get("symbol"),
        "best_bid": data.get("bid"),
        "mid": data.get("mid"),
        "has_position": bool(data.get("has_position")),
        "trigger_move_pct": getattr(t, "trigger_move_pct", None),
        "trigger_window_sec": getattr(t, "trigger_window_sec", None),
        "trigger_fired": bool(data.get("trigger_fired")),
    }
    move = data.get("move_pct")
    payload["observed_move_pct_signed"] = move
    payload["move_fraction_of_threshold"] = data.get("move_frac")
    payload["cooldown_remaining_sec"] = data.get("cooldown_remaining")
    if data.get("has_position"):
        payload["entry"] = data.get("entry")
        payload["sl_price"] = data.get("sl_price")
        payload["tp_price"] = data.get("tp_price")
        payload["distance_to_sl_pct"] = data.get("dist_sl_pct")
        payload["distance_to_tp_pct"] = data.get("dist_tp_pct")
    return payload


def _f(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


class Guardian:
    """Daemon thread watching positions, balances, triggers and heartbeats."""

    def __init__(self, cfg: Any, db: Any, exchange: Any, executor: Any,
                 on_trigger: Callable[[str], None],
                 clock: Any = time.time, sleep: Any = time.sleep,
                 run_id: Optional[int] = None):
        self.cfg = cfg
        self.db = db
        self.exchange = exchange
        self.executor = executor
        self.on_trigger = on_trigger
        self.clock = clock
        self.sleep = sleep
        self.run_id = run_id
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_balance = float("-inf")
        self._last_heartbeat = float("-inf")
        self._prices: Dict[str, collections.deque] = {}
        self._last_trigger: Dict[str, float] = {}
        self._consecutive_429 = 0
        self._backoff_until = 0.0
        self._tick_saw_429 = False
        # observability (v2.2): latest per-symbol watch data + throttles
        self._watch: Dict[str, Dict[str, Any]] = {}
        self._last_watch_event: Dict[str, float] = {}
        self._last_watch_console: Dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="guardian", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ------------------------------------------------------------------ #
    # infra
    # ------------------------------------------------------------------ #
    def _event(self, level: str, kind: str, message: str,
               data: Optional[Dict[str, Any]] = None) -> None:
        try:
            self.db.log_event(level, kind, message, data, run_id=self.run_id)
        except Exception:
            pass  # logging must never crash the guardian

    def _register_error(self, exc: BaseException) -> None:
        """A 429 backs this thread off for the tick; anything else is just logged."""
        if getattr(exc, "http_status", None) == 429:
            self._tick_saw_429 = True
            t = self.cfg.trading
            self._consecutive_429 += 1
            delay = min(float(t.ratelimit_backoff_cap_sec),
                        float(t.ratelimit_backoff_start_sec) * (2 ** (self._consecutive_429 - 1)))
            self._backoff_until = self.clock() + delay
            self._event("WARN", "RATE_LIMIT",
                        f"429 while guarding; backing off {delay:.0f}s", {})

    def _sleep_interruptible(self, seconds: float) -> None:
        remaining = max(0.0, float(seconds))
        while remaining > 0.0 and not self._stop_event.is_set():
            chunk = min(0.5, remaining)
            self.sleep(chunk)
            remaining -= chunk

    def _run(self) -> None:
        """Supervisor: restart the loop and log it if it ever exits."""
        while not self._stop_event.is_set():
            try:
                self._loop()
            except BaseException as exc:  # truly unexpected: loop-level crash
                self._event("ERROR", "GUARDIAN_RESTART",
                            f"guardian loop exited unexpectedly: {exc}; restarting",
                            {"traceback": "".join(traceback.format_exc(limit=6))})
                self._sleep_interruptible(1.0)

    def _loop(self) -> None:
        interval = float(self.cfg.trading.GUARDIAN_INTERVAL_SEC)
        while not self._stop_event.is_set():
            started = self.clock()
            try:
                self.tick()
            except Exception as exc:
                self._register_error(exc)
                self._event("ERROR", "GUARDIAN_TICK_ERROR",
                            f"guardian tick crashed: {exc}",
                            {"traceback": "".join(traceback.format_exc(limit=6))})
            elapsed = self.clock() - started
            delay = max(0.0, interval - elapsed)
            if self._backoff_until > self.clock():
                delay = max(delay, self._backoff_until - self.clock())
            self._sleep_interruptible(delay)

    # ------------------------------------------------------------------ #
    # one tick
    # ------------------------------------------------------------------ #
    def tick(self) -> Dict[str, Any]:
        t = self.cfg.trading
        now = self.clock()
        summary: Dict[str, Any] = {"closed": 0, "triggered": []}
        self._tick_saw_429 = False
        try:
            return self._tick_inner(now, summary)
        finally:
            # a clean tick (no 429 anywhere) resets the escalation counter;
            # _backoff_until expires on its own and keeps slowing the next sleep
            if not self._tick_saw_429:
                self._consecutive_429 = 0

    def _tick_inner(self, now: float, summary: Dict[str, Any]) -> Dict[str, Any]:
        t = self.cfg.trading

        # 1. paper mode: let resting limit orders fill against fresh depth
        check_open = getattr(self.exchange, "check_open_orders", None)
        if callable(check_open):
            try:
                check_open()
            except Exception as exc:
                self._register_error(exc)
                self._event("WARN", "GUARDIAN_TICK_ERROR",
                            f"paper order sync failed: {exc}", {})

        # 2. SL/TP per open position
        try:
            positions = self.db.get_open_positions(bool(t.dry_run))
        except Exception as exc:
            self._register_error(exc)
            self._event("ERROR", "GUARDIAN_TICK_ERROR",
                        f"cannot read open positions: {exc}", {})
            positions = []
        open_symbols = set()
        for pos in positions:
            symbol = pos.get("symbol")
            open_symbols.add(symbol)
            try:
                if self._check_position(pos):
                    summary["closed"] += 1
            except Exception as exc:
                self._register_error(exc)
                self._event("ERROR", "GUARDIAN_TICK_ERROR",
                            f"SL/TP check failed for {symbol}: {exc}",
                            {"symbol": symbol,
                             "traceback": "".join(traceback.format_exc(limit=6))})

        # 3. balance snapshot
        if now - self._last_balance >= float(t.balance_snapshot_interval_sec):
            self._last_balance = now
            self._balance_snapshot()

        # 4. event trigger for symbols WITHOUT an open position
        if t.event_trigger_enabled:
            for symbol in t.SYMBOLS:
                if symbol in open_symbols:
                    self._prices.pop(symbol, None)  # stale moves never fire later
                    continue
                try:
                    if self._trigger_check(symbol, now):
                        summary["triggered"].append(symbol)
                except Exception as exc:
                    self._register_error(exc)
                    self._event("ERROR", "GUARDIAN_TICK_ERROR",
                                f"trigger check failed for {symbol}: {exc}",
                                {"symbol": symbol})

        # 5. heartbeat
        if now - self._last_heartbeat >= float(t.heartbeat_interval_sec):
            self._last_heartbeat = now
            self._event("INFO", "HEARTBEAT",
                        f"guardian alive; watching {len(positions)} open position(s)",
                        {"open_positions": sorted(s for s in open_symbols if s)})
        # observability: throttled per-symbol watch lines + GUARDIAN_WATCH events
        # (pure logging; no effect on SL/TP timing or trigger decisions)
        try:
            self._emit_watch(now)
        except Exception:
            pass
        return summary

    # ------------------------------------------------------------------ #
    # observability: per-symbol watch (console + events), throttled
    # ------------------------------------------------------------------ #
    def _emit_watch(self, now: float) -> None:
        t = self.cfg.trading
        verbosity = int(getattr(t, "CONSOLE_VERBOSITY", 1))
        for symbol in t.SYMBOLS:
            data = self._watch.get(symbol)
            if data is None:
                continue
            line = format_watch_line(data)
            try:
                if verbosity >= 2:
                    _console(line)  # verbose: compact line every tick
                elif verbosity >= 1 and now - self._last_watch_console.get(
                        symbol, float("-inf")) >= float(t.guardian_console_interval_sec):
                    self._last_watch_console[symbol] = now
                    _console(line)
            except Exception:
                pass
            if now - self._last_watch_event.get(
                    symbol, float("-inf")) >= float(t.guardian_watch_log_interval_sec):
                self._last_watch_event[symbol] = now
                try:
                    self.db.log_event("INFO", "GUARDIAN_WATCH", line,
                                      watch_event_data(data, t),
                                      run_id=self.run_id)
                except Exception:
                    pass  # logging must never crash the guardian

    # ------------------------------------------------------------------ #
    # SL / TP
    # ------------------------------------------------------------------ #
    def _best_bid(self, symbol: str) -> Optional[float]:
        """One depth call per symbol -- the cheapest executable price read."""
        try:
            depth = self.exchange.get_depth(symbol)
        except Exception as exc:
            self._register_error(exc)
            self._event("WARN", "GUARDIAN_TICK_ERROR",
                        f"price read failed for {symbol}: {exc}", {"symbol": symbol})
            return None
        bids = depth.get("bids") or []
        return float(bids[0][0]) if bids else None

    def _check_position(self, pos: Dict[str, Any]) -> bool:
        """Close on SL/TP using the stored prices (computed at entry from the
        guard-selected percentages). Returns True when a close was started."""
        symbol = pos.get("symbol")
        if not symbol or self.executor.is_closing(symbol):
            return False
        entry = _f(pos.get("entry_price"))
        stop_price = pos.get("stop_price")
        if stop_price is None:
            stop_price = entry * (1.0 - _f(pos.get("stop_pct")))
        target_price = pos.get("target_price")
        if target_price is None:
            target_price = entry * (1.0 + _f(pos.get("target_pct")))
        bid = self._best_bid(symbol)
        if bid is None:
            return False
        # observability: stash the position's watch data (pure logging)
        dist_sl = ((bid - float(stop_price)) / entry * 100.0) \
            if (entry > 0 and stop_price) else None
        dist_tp = ((float(target_price) - bid) / entry * 100.0) \
            if (entry > 0 and target_price) else None
        try:
            self._watch[symbol] = {
                "symbol": symbol, "bid": bid, "mid": bid, "has_position": True,
                "entry": entry, "sl_price": stop_price, "tp_price": target_price,
                "dist_sl_pct": dist_sl, "dist_tp_pct": dist_tp,
                "ok": not (stop_price and bid <= float(stop_price) + EPS) and
                      not (target_price and bid >= float(target_price) - EPS),
            }
        except Exception:
            pass
        if stop_price and bid <= float(stop_price) + EPS:
            self._event("INFO", "STOP_LOSS",
                        f"stop-loss hit on {symbol}: bid {bid} <= stop {stop_price}",
                        {"symbol": symbol, "bid": bid, "stop_price": stop_price})
            _console(f"[CLOSE] {symbol} SL hit: bid={bid} <= sl={stop_price}")
            res = self.executor.close_position(symbol, "SL")
            _console(f"[CLOSE] {symbol} SL executed: {res.action} "
                     f"({res.reason}) pnl={res.pnl_tmn}")
            return bool(res.ok)
        if target_price and bid >= float(target_price) - EPS:
            self._event("INFO", "TAKE_PROFIT",
                        f"take-profit hit on {symbol}: bid {bid} >= target {target_price}",
                        {"symbol": symbol, "bid": bid, "target_price": target_price})
            _console(f"[CLOSE] {symbol} TP hit: bid={bid} >= tp={target_price}")
            res = self.executor.close_position(symbol, "TP")
            _console(f"[CLOSE] {symbol} TP executed: {res.action} "
                     f"({res.reason}) pnl={res.pnl_tmn}")
            return bool(res.ok)
        return False

    # ------------------------------------------------------------------ #
    # balance snapshot
    # ------------------------------------------------------------------ #
    def _balance_snapshot(self) -> None:
        try:
            snap = self.executor.snapshot()
        except Exception as exc:
            self._register_error(exc)
            self._event("WARN", "GUARDIAN_TICK_ERROR",
                        f"balance snapshot failed: {exc}", {})
            return
        if snap.get("total_value_tmn") is None:
            return  # snapshot() already logged the reason
        mode = "paper" if bool(self.cfg.trading.dry_run) else "live"
        tmn = (snap.get("assets") or {}).get("TMN") or {}
        try:
            self.db.insert_balance_snapshot(
                run_id=self.run_id, mode=mode, tmn_balance=tmn.get("free"),
                assets=snap.get("assets"),
                total_value_tmn=snap.get("total_value_tmn"))
        except Exception as exc:
            self._register_error(exc)
            self._event("WARN", "GUARDIAN_TICK_ERROR",
                        f"balance snapshot row failed: {exc}", {})

    # ------------------------------------------------------------------ #
    # event trigger (no models: it only wakes the decision cycle)
    # ------------------------------------------------------------------ #
    def _trigger_check(self, symbol: str, now: float) -> bool:
        t = self.cfg.trading
        depth = self.exchange.get_depth(symbol)
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        cooldown_remaining = max(0.0, float(t.trigger_cooldown_sec)
                                 - (now - self._last_trigger.get(symbol, 0.0)))
        if not bids or not asks:
            try:
                self._watch[symbol] = {"symbol": symbol, "bid": None, "mid": None,
                                       "has_position": False, "trigger_fired": False,
                                       "cooldown_remaining": cooldown_remaining,
                                       "threshold_pct": float(t.trigger_move_pct) * 100.0}
            except Exception:
                pass
            return False
        bid = float(bids[0][0])
        mid = (bid + float(asks[0][0])) / 2.0
        window = self._prices.setdefault(symbol, collections.deque())
        window.append((now, mid))
        max_age = float(t.trigger_window_sec)
        while window and now - window[0][0] > max_age:
            window.popleft()
        threshold = float(t.trigger_move_pct)

        def _stash(move_signed, move_frac, fired):
            try:
                self._watch[symbol] = {
                    "symbol": symbol, "bid": bid, "mid": mid, "has_position": False,
                    "move_pct": move_signed, "threshold_pct": threshold * 100.0,
                    "move_frac": move_frac, "cooldown_remaining": cooldown_remaining,
                    "trigger_fired": fired,
                }
            except Exception:
                pass

        if len(window) < 2:
            _stash(None, None, False)
            return False
        first_price = window[0][1]
        if first_price <= 0:
            _stash(None, None, False)
            return False
        move = abs(mid - first_price) / first_price
        move_signed = (mid - first_price) / first_price * 100.0
        move_frac = move / threshold if threshold > 0 else None
        if move <= threshold:
            _stash(move_signed, move_frac, False)
            return False
        if now - self._last_trigger.get(symbol, 0.0) < float(t.trigger_cooldown_sec):
            _stash(move_signed, move_frac, False)
            return False
        _stash(move_signed, move_frac, True)
        self._last_trigger[symbol] = now
        window.clear()  # never re-fire off the same move
        self._event("INFO", "TRIGGER",
                    f"price moved {move * 100:.2f}% on {symbol} within "
                    f"{t.trigger_window_sec}s; requesting a decision cycle",
                    {"symbol": symbol, "move_pct": move, "window_sec": t.trigger_window_sec})
        _console(f"[TRIGGER] {symbol} moved {move * 100:.2f}% in "
                 f"{t.trigger_window_sec}s -> early decision cycle")
        try:
            self.on_trigger(symbol)
        except Exception as exc:
            self._event("ERROR", "GUARDIAN_TICK_ERROR",
                        f"on_trigger callback failed for {symbol}: {exc}",
                        {"symbol": symbol})
            return False
        return True
