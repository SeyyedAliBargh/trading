"""Shared fakes for the STEP P-B tests: clock, exchange, LLM client.

No network, no real sleeping. The FakeExchange mimics the parts of the
exchange contract the executor/guardian use (depth, market info, balances,
create/get/cancel order, open orders) with injectable failures.
"""
import threading
import uuid
from typing import Any, Dict, List, Optional

from exchange import book as book_mod
from exchange.wallex import MarketInfo, WallexError, base_asset


class FakeClock:
    """Injectable clock: sleep() advances fake time and records the sleeps."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = float(start)
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self.now += float(seconds)

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeExchange:
    """In-memory exchange with injectable failure modes.

    Defaults: huge depth around 100/101, TMN 5M, fee 0, MARKET works.
    Hooks (set per test):
      create_errors        deque of exceptions raised by the next create_order calls
      partial_next         fraction of the NEXT created order filled immediately
                            (order stays ACTIVE with that executedQty)
      fill_after_polls     {client_id: n} -> full fill on the n-th get_order
      cancel_fills_mode    cancel_order fills the order fully FIRST, then raises
      market_rejected      symbols whose MARKET create raises 400 "market disabled"
      market_needs_price   symbols whose MARKET without price raises 400 "price required"
      depth_errors / candles_errors / balances_error   read failures
      post_create          callable(fe, client_id) run after create_order
      create_gate          threading.Event; when set, create_order waits on it
    """

    def __init__(self, symbols: List[str], price_precision: int = 0,
                 amount_precision: int = 8, fee_rate: float = 0.0):
        self.symbols = list(symbols)
        self.pp = price_precision
        self.ap = amount_precision
        self.fee_rate = fee_rate
        default = {"bids": [(100.0, 10_000.0, 1_000_000.0), (99.0, 10_000.0, 990_000.0)],
                   "asks": [(101.0, 10_000.0, 1_010_000.0), (102.0, 10_000.0, 1_020_000.0)]}
        self.books: Dict[str, Dict[str, List]] = {s: {"bids": list(default["bids"]),
                                                      "asks": list(default["asks"])}
                                                  for s in self.symbols}
        self.balances: Dict[str, Dict[str, float]] = {"TMN": {"free": 5_000_000.0,
                                                             "locked": 0.0}}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.create_errors: List[BaseException] = []
        self.cancel_errors: Dict[str, BaseException] = {}
        self.get_order_errors: Dict[str, BaseException] = {}
        self.fill_after_polls: Dict[str, int] = {}
        self.poll_counts: Dict[str, int] = {}
        self.partial_next: Optional[float] = None
        self.no_fill_at_create = False  # orders rest even when they cross
        self.cancel_fills_mode = False
        self.market_rejected: set = set()
        self.market_needs_price: set = set()
        self.depth_errors: Dict[str, BaseException] = {}
        self.candles_errors: Dict[str, BaseException] = {}
        self.balances_error: Optional[BaseException] = None
        self.post_create = None
        self.create_gate: Optional[threading.Event] = None
        self._seq = 0

    # -- reads ---------------------------------------------------------------
    def get_depth(self, symbol: str) -> Dict[str, List]:
        if symbol in self.depth_errors:
            raise self.depth_errors[symbol]
        book = self.books.setdefault(symbol, {
            "bids": [(100.0, 10_000.0, 1_000_000.0)],
            "asks": [(101.0, 10_000.0, 1_010_000.0)]})
        return {"bids": [tuple(x) for x in book["bids"]],
                "asks": [tuple(x) for x in book["asks"]]}

    def get_market_info(self, symbol: str):
        return MarketInfo(symbol=symbol, base_asset=base_asset(symbol),
                          quote_asset="TMN", price_precision=self.pp,
                          amount_precision=self.ap,
                          is_market_type_enable=symbol not in self.market_rejected,
                          is_zero_fee=False, flags=[])

    def get_fee_for(self, symbol: str) -> Dict[str, float]:
        return {"maker_rate": self.fee_rate, "taker_rate": self.fee_rate}

    def get_balances(self) -> Dict[str, Dict[str, float]]:
        if self.balances_error is not None:
            raise self.balances_error
        return {a: dict(v) for a, v in self.balances.items()}

    def get_candles_1m(self, symbol: str, from_ts: int, to_ts: int) -> List[Dict]:
        if symbol in self.candles_errors:
            raise self.candles_errors[symbol]
        base = int(from_ts) - (int(from_ts) % 60)
        return [{"t": base + i * 60, "o": 100.0, "h": 101.0, "l": 99.0,
                 "c": 100.5, "v": 10.0} for i in range(60)]

    def get_candles_1h(self, symbol: str, from_ts: int, to_ts: int) -> List[Dict]:
        if symbol in self.candles_errors:
            raise self.candles_errors[symbol]
        base = int(from_ts) - (int(from_ts) % 3600)
        return [{"t": base + i * 3600, "o": 100.0, "h": 110.0, "l": 95.0,
                 "c": 105.0, "v": 500.0} for i in range(24)]

    def get_open_orders(self, symbol: Optional[str] = None) -> Dict:
        orders = [dict(o) for o in self.orders.values()
                  if o.get("active") and (symbol is None or o["symbol"] == symbol)]
        return {"orders": orders, "raw": {}}

    # -- order lifecycle ---------------------------------------------------------
    def _mk_order(self, symbol, side, type_, qty, price, client_id, filled, avg):
        fee = filled * avg * self.fee_rate  # TMN-equivalent (fee in base on BUY)
        status = "FILLED" if filled >= qty - 1e-9 else (
            "PARTIALLY_FILLED" if filled > 0 else "NEW")
        return {
            "active": filled < qty - 1e-9,
            "clientOrderId": client_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "executedQty": filled,
            "executedPrice": avg if filled > 0 else None,
            "executedSum": filled * avg if filled > 0 else 0.0,
            "fee": fee,
            "fills": [] if filled <= 0 else [{"price": avg, "quantity": filled}],
            "origQty": qty,
            "price": price,
            "side": side,
            "status": status,
            "symbol": symbol,
            "type": type_,
        }

    def _apply_fill(self, order: Dict[str, Any], add_qty: float, price: float) -> None:
        base = base_asset(order["symbol"])
        self.balances.setdefault(base, {"free": 0.0, "locked": 0.0})
        if order["side"] == "BUY":
            self.balances["TMN"]["free"] -= add_qty * price
            self.balances[base]["free"] += add_qty * (1.0 - self.fee_rate)
        else:
            self.balances[base]["free"] -= add_qty
            self.balances["TMN"]["free"] += add_qty * price * (1.0 - self.fee_rate)
        order["executedQty"] = float(order["executedQty"]) + add_qty
        order["executedPrice"] = price
        order["executedSum"] = float(order["executedQty"]) * price
        order["fee"] = float(order.get("fee") or 0.0) + add_qty * price * self.fee_rate
        if order["executedQty"] >= float(order["origQty"]) - 1e-9:
            order["active"] = False
            order["status"] = "FILLED"
        elif order["executedQty"] > 0:
            order["status"] = "PARTIALLY_FILLED"

    def create_order(self, symbol: str, side: str, type: str,
                     quantity: Optional[float] = None, price: Optional[float] = None,
                     stop_price: Optional[float] = None,
                     client_id: Optional[str] = None) -> Dict:
        self.calls.append({"symbol": symbol, "side": (side or "").upper(),
                           "type": (type or "").upper(), "quantity": quantity,
                           "price": price, "client_id": client_id})
        if self.create_gate is not None:
            self.create_gate.wait(timeout=5)
        if self.create_errors:
            raise self.create_errors.pop(0)
        side_u, type_u = (side or "").upper(), (type or "").upper()
        if client_id is None:
            self._seq += 1
            client_id = f"ag_fake_{self._seq}_{uuid.uuid4().hex[:6]}"
        if type_u == "MARKET" and symbol in self.market_rejected:
            raise WallexError(f"HTTP 400: market type disabled for {symbol}",
                              http_status=400)
        if type_u == "MARKET" and price is None and symbol in self.market_needs_price:
            raise WallexError("HTTP 400: price is required for market order",
                              http_status=400)
        book = self.books.setdefault(symbol, {"bids": [(100.0, 10_000.0, 1_000_000.0)],
                                              "asks": [(101.0, 10_000.0, 1_010_000.0)]})
        levels = book["asks"] if side_u == "BUY" else book["bids"]
        match = book_mod.fill_qty(levels, float(quantity),
                                 max_price=price if (type_u == "LIMIT" and side_u == "BUY") else None,
                                 min_price=price if (type_u == "LIMIT" and side_u == "SELL") else None)
        filled = match["filled_qty"]
        avg = match["avg_price"] or (price or 0.0)
        if self.no_fill_at_create:
            filled = 0.0  # order rests even though it crosses
        elif self.partial_next is not None:
            # only the given fraction fills now; the order stays ACTIVE
            filled = min(filled, float(quantity) * self.partial_next)
            self.partial_next = None
        order = self._mk_order(symbol, side_u, type_u, float(quantity), price,
                               client_id, 0.0, 0.0)
        self.orders[client_id] = order
        if filled > 0:
            self._apply_fill(order, filled, avg)
        if self.post_create is not None:
            self.post_create(self, client_id)
        return {"order": dict(order), "raw": {}}

    def _fill_px(self, order: Dict[str, Any]) -> float:
        px = float(order.get("price") or 0.0)
        if px <= 0:
            book = self.books[order["symbol"]]
            px = float(book["bids"][0][0]) if order["side"] == "SELL" \
                else float(book["asks"][0][0])
        return px

    def get_order(self, client_id: str) -> Dict:
        if client_id in self.get_order_errors:
            raise self.get_order_errors[client_id]
        order = self.orders.get(client_id)
        if order is None:
            raise RuntimeError(f"order not found: {client_id}")
        n = self.fill_after_polls.get(client_id)
        if n is not None and order.get("active"):
            self.poll_counts[client_id] = self.poll_counts.get(client_id, 0) + 1
            if self.poll_counts[client_id] >= n:
                remaining = float(order["origQty"]) - float(order["executedQty"])
                if remaining > 0:
                    self._apply_fill(order, remaining, self._fill_px(order))
        return {"order": dict(order), "raw": {}}

    def cancel_order(self, client_id: str) -> Dict:
        if client_id in self.cancel_errors:
            raise self.cancel_errors[client_id]
        order = self.orders.get(client_id)
        if order is None:
            raise RuntimeError(f"order not found: {client_id}")
        if not order.get("active"):
            raise RuntimeError("cannot cancel a finished order")
        if self.cancel_fills_mode:
            # race: the order fills fully during the cancel attempt
            remaining = float(order["origQty"]) - float(order["executedQty"])
            if remaining > 0:
                self._apply_fill(order, remaining, self._fill_px(order))
            raise RuntimeError("cancel failed: order filled during cancel")
        order["active"] = False
        order["status"] = "CANCELED"  # partial fills are kept in executedQty
        return {"order": dict(order), "raw": {}}


class FakeLlmClient:
    """Stands in for main.BoxApiClient; logs llm_calls rows like the real one."""

    def __init__(self, cfg: Any = None, db: Any = None, transport: Any = None,
                 actions: Optional[List[str]] = None):
        self.cfg = cfg
        self.db = db
        self.actions = actions if actions is not None else ["BUY", "BUY", "HOLD"]
        self.calls: List[Dict[str, Any]] = []

    def decide_all(self, input_json: Dict[str, Any], models: Optional[List[str]] = None,
                   decision_id: Optional[int] = None,
                   run_id: Optional[int] = None) -> List[Any]:
        self.calls.append({"input_json": input_json, "decision_id": decision_id,
                           "run_id": run_id})
        votes = []
        for i, action in enumerate(self.actions):
            votes.append(_mk_vote(f"m{i}", action))
            if self.db is not None:
                try:
                    self.db.insert_llm_call(
                        run_id=run_id, decision_id=decision_id, model=f"m{i}",
                        raw_output='{"action": "%s"}' % action,
                        parsed_json={"action": action}, valid=True,
                        prompt_tokens=100, completion_tokens=20, latency_ms=5.0)
                except Exception:
                    pass
        return votes


def _mk_vote(model: str, action: str, confidence: float = 0.7,
             stop: float = 0.015, target: float = 0.045, valid: bool = True,
             error: Optional[str] = None):
    from ai.models import ModelVote
    return ModelVote(model=model, action=action, confidence=confidence,
                     stop_pct=stop, target_pct=target, reason="test vote",
                     valid=valid, error=error)
