"""Paper trading exchange simulator (STEP 4b).

Simulates against the REAL order book obtained from an injected read-only
data source (a WallexClient used only for GET calls). All order state is
persisted in the store; critical write failures raise PaperExchangeError.
"""
import time
import uuid
from typing import Any, Dict, List, Optional

import config
import store.db as store_db
from exchange import book
from exchange.wallex import WallexError, base_asset


class PaperExchangeError(Exception):
    """Raised when a critical store write fails or the wallet cannot cover an order."""


# Paper-only order status strings (raw values; executors must rely on
# `active` and `executedQty`, never on status names).
class PaperStatus:
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


_OPEN_STATUSES = (PaperStatus.NEW, PaperStatus.PARTIALLY_FILLED)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _assets_get(assets: Dict[str, Any], key: str) -> Dict[str, float]:
    entry = assets.get(key)
    if not isinstance(entry, dict):
        return {"free": 0.0, "locked": 0.0}
    return {"free": float(entry.get("free", 0.0)), "locked": float(entry.get("locked", 0.0))}


def _assets_set(assets: Dict[str, Any], key: str, free: float, locked: float) -> None:
    if abs(free) < 1e-12 and abs(locked) < 1e-12:
        assets.pop(key, None)
    else:
        assets[key] = {"free": free, "locked": locked}


class PaperExchange:
    """Simulated exchange for DRY_RUN mode. No network calls for orders."""

    def __init__(self, cfg: config.Config, db_path: str, data_source):
        self.cfg = cfg
        self.db_path = db_path
        self.data_source = data_source
        store_db.init_db(db_path)
        acct = store_db.get_paper_account()
        if acct is None:
            if not store_db.reset_paper_account(cfg.trading.paper_initial_capital_tmn):
                raise PaperExchangeError("Failed to initialise paper account")

    # ------------------------------------------------------------------ #
    # Account / balances
    # ------------------------------------------------------------------ #
    def _account(self) -> Dict[str, Any]:
        acct = store_db.get_paper_account()
        if acct is None:
            raise PaperExchangeError("Paper account missing")
        assets = acct.get("assets", {})
        if not isinstance(assets, dict):
            assets = {}
        # ensure TMN entry exists and mirrors tmn_balance
        tmn = _assets_get(assets, "TMN")
        assets["TMN"] = {"free": float(acct["tmn_balance"]), "locked": tmn["locked"]}
        return {"tmn_balance": float(acct["tmn_balance"]), "assets": assets}

    def _save_account(self, tmn_free: float, assets: Dict[str, Any]) -> None:
        if not store_db.set_paper_account(tmn_free, assets):
            raise PaperExchangeError("Failed to persist paper account")

    def _apply_deltas(self, free_deltas: Dict[str, float],
                      lock_deltas: Dict[str, float]) -> None:
        """Apply balance deltas; raises PaperExchangeError if any goes negative."""
        acct = self._account()
        assets = acct["assets"]
        # validate first
        for key, d in {**free_deltas, **{k: 0.0 for k in lock_deltas}}.items():
            cur = _assets_get(assets, key)
            nf = cur["free"] + free_deltas.get(key, 0.0)
            nl = cur["locked"] + lock_deltas.get(key, 0.0)
            if nf < -1e-9 or nl < -1e-9:
                raise PaperExchangeError(f"Insufficient {key} balance")
        for key in set(list(free_deltas) + list(lock_deltas)):
            cur = _assets_get(assets, key)
            _assets_set(assets, key,
                        cur["free"] + free_deltas.get(key, 0.0),
                        cur["locked"] + lock_deltas.get(key, 0.0))
        tmn = _assets_get(assets, "TMN")
        self._save_account(tmn["free"], assets)

    def get_balances(self) -> Dict[str, Dict[str, float]]:
        acct = self._account()
        out: Dict[str, Dict[str, float]] = {}
        for key, val in acct["assets"].items():
            out[key] = {"free": float(val.get("free", 0.0)), "locked": float(val.get("locked", 0.0))}
        out.setdefault("TMN", {"free": 0.0, "locked": 0.0})
        return out

    # ------------------------------------------------------------------ #
    # Fees
    # ------------------------------------------------------------------ #
    def _taker_fee_for(self, symbol: str) -> float:
        try:
            info = self.data_source.get_fee_for(symbol)
            if info and info.get("taker_rate") is not None:
                return float(info["taker_rate"])
        except Exception:
            pass
        return float(self.cfg.trading.paper_fallback_fee_rate)

    # ------------------------------------------------------------------ #
    # Depth
    # ------------------------------------------------------------------ #
    def _depth(self, symbol: str) -> Dict[str, List]:
        return self.data_source.get_depth(symbol)

    # ------------------------------------------------------------------ #
    # Order helpers
    # ------------------------------------------------------------------ #
    def _next_client_id(self, symbol: str) -> str:
        return f"paper_{symbol}_{uuid.uuid4().hex[:12]}"

    def _persist_order(self, *, client_id, symbol, side, type_, price, quantity,
                       status, filled_qty, avg_price, fee_tmn, fills, orig_qty) -> int:
        order_id = store_db.insert_order(
            run_id=None, client_id=client_id, symbol=symbol, side=side, type=type_,
            price=price, quantity=orig_qty, status=status,
            filled_quantity=filled_qty, avg_fill_price=avg_price, fee_tmn=fee_tmn,
            dry_run=True,
            request_json={"side": side, "symbol": symbol, "type": type_,
                          "price": price, "quantity": quantity},
            response_json={"fills": fills, "status": status},
        )
        if order_id is None:
            raise PaperExchangeError(f"Failed to persist order {client_id}")
        return order_id

    def _update_order_row(self, order_row_id: int, **fields) -> None:
        if not store_db.update_order(order_row_id, **fields):
            raise PaperExchangeError(f"Failed to update order row {order_row_id}")

    @staticmethod
    def _order_dict(row: Dict[str, Any], fills: Optional[List[Dict]] = None) -> Dict[str, Any]:
        status = row.get("status")
        active = status in _OPEN_STATUSES
        return {
            "active": active,
            "clientOrderId": row.get("client_id"),
            "created_at": row.get("ts"),
            "executedPercent": None,
            "executedPrice": row.get("avg_fill_price"),
            "executedQty": row.get("filled_quantity"),
            "executedSum": (row.get("avg_fill_price") or 0) * (row.get("filled_quantity") or 0),
            "fee": row.get("fee_tmn"),
            "fills": fills if fills is not None else [],
            "origQty": row.get("quantity"),
            "price": row.get("price"),
            "side": row.get("side"),
            "status": status,
            "stopPrice": None,
            "stopPriceCondition": None,
            "sum": (row.get("price") or 0) * (row.get("quantity") or 0),
            "symbol": row.get("symbol"),
            "transactTime": None,
            "type": row.get("type"),
            "updated_at": row.get("updated_at"),
        }

    # ------------------------------------------------------------------ #
    # create_order
    # ------------------------------------------------------------------ #
    def create_order(self, symbol: str, side: str, type: str,
                     quantity: Optional[float] = None, price: Optional[float] = None,
                     stop_price: Optional[float] = None,
                     client_id: Optional[str] = None) -> Dict:
        side_u = side.upper()
        type_u = type.upper()
        if side_u not in ("BUY", "SELL"):
            raise PaperExchangeError("Invalid side")
        if type_u not in ("LIMIT", "MARKET"):
            raise PaperExchangeError(f"Unsupported order type for paper: {type_u}")
        if quantity is None or quantity <= 0:
            raise PaperExchangeError("quantity must be positive")
        if type_u == "LIMIT" and price is None:
            raise PaperExchangeError("LIMIT order requires price")
        if client_id is None:
            client_id = self._next_client_id(symbol)

        base = base_asset(symbol)
        fee_rate = self._taker_fee_for(symbol)
        depth = self._depth(symbol)

        if side_u == "BUY":
            levels = depth.get("asks", [])
            cap = price if type_u == "LIMIT" else None   # ask must be <= limit
            floor = None
        else:
            levels = depth.get("bids", [])
            cap = None
            floor = price if type_u == "LIMIT" else None  # bid must be >= limit

        match = book.fill_qty(levels, quantity, max_price=cap, min_price=floor)
        filled_qty = match["filled_qty"]
        fills = match["fills"]
        avg_price = match["avg_price"] or 0.0
        notional = filled_qty * avg_price

        fee_in_base = (side_u == "BUY" and self.cfg.trading.paper_buy_fee_in_base)
        if fee_in_base:
            fee_qty = filled_qty * fee_rate
            fee_tmn = fee_qty * avg_price  # informational
        else:
            fee_tmn = notional * fee_rate

        # ---- balance effects ----------------------------------------
        free_deltas: Dict[str, float] = {}
        lock_deltas: Dict[str, float] = {}
        if side_u == "BUY":
            cost = notional
            if not fee_in_base:
                cost += fee_tmn
            free_deltas["TMN"] = -cost
            credit = filled_qty - fee_qty if fee_in_base else filled_qty
            free_deltas[base] = credit
            remainder = quantity - filled_qty
            if remainder > 1e-9 and type_u == "LIMIT":
                lock_deltas["TMN"] = remainder * price
        else:  # SELL
            free_deltas[base] = -filled_qty
            free_deltas["TMN"] = notional - fee_tmn
            remainder = quantity - filled_qty
            if remainder > 1e-9 and type_u == "LIMIT":
                lock_deltas[base] = remainder

        try:
            self._apply_deltas(free_deltas, lock_deltas)
        except PaperExchangeError:
            raise

        if filled_qty >= quantity - 1e-9:
            status = PaperStatus.FILLED
        elif filled_qty > 0:
            status = PaperStatus.PARTIALLY_FILLED
        else:
            status = PaperStatus.NEW

        fill_dicts = []
        for p, q in fills:
            fill_dicts.append({
                "fee": q * fee_rate * (p if not fee_in_base else 1.0),
                "feeAsset": base if fee_in_base else "TMN",
                "price": p,
                "quantity": q,
                "sum": p * q,
                "symbol": symbol,
                "isBuyer": side_u == "BUY",
                "timestamp": _now_iso(),
            })

        order_row_id = self._persist_order(
            client_id=client_id, symbol=symbol, side=side_u, type_=type_u,
            price=price, quantity=quantity, status=status,
            filled_qty=filled_qty, avg_price=avg_price, fee_tmn=fee_tmn,
            fills=fill_dicts, orig_qty=quantity,
        )
        row = store_db.get_order_by_client_id(client_id)
        order = self._order_dict(row, fill_dicts) if row else {}
        return {"order": order, "raw": {"order_row_id": order_row_id}}

    # ------------------------------------------------------------------ #
    # cancel / get / list
    # ------------------------------------------------------------------ #
    def cancel_order(self, client_id: str) -> Dict:
        row = store_db.get_order_by_client_id(client_id)
        if row is None:
            raise PaperExchangeError("Order not found")
        status = row.get("status")
        if status in (PaperStatus.FILLED, PaperStatus.CANCELED):
            raise PaperExchangeError("Cannot cancel a finished order")
        filled = float(row.get("filled_quantity") or 0.0)
        orig = float(row.get("quantity") or 0.0)
        remainder = orig - filled
        side = row.get("side")
        price = float(row.get("price") or 0.0)
        symbol = row.get("symbol")
        base = base_asset(symbol)
        if remainder > 1e-9:
            if side == "BUY":
                self._apply_deltas({}, {"TMN": -remainder * price})
            else:
                self._apply_deltas({}, {base: -remainder})
        self._update_order_row(row["id"], status=PaperStatus.CANCELED)
        row = store_db.get_order_by_client_id(client_id)
        return {"order": self._order_dict(row), "raw": {}}

    def get_order(self, client_id: str) -> Dict:
        row = store_db.get_order_by_client_id(client_id)
        if row is None:
            raise PaperExchangeError("Order not found")
        return {"order": self._order_dict(row), "raw": {}}

    def get_open_orders(self, symbol: Optional[str] = None) -> Dict:
        rows = store_db.get_orders_by_status(list(_OPEN_STATUSES), dry_run=True)
        if symbol:
            rows = [r for r in rows if r.get("symbol") == symbol]
        return {"orders": [self._order_dict(r) for r in rows], "raw": {}}

    # ------------------------------------------------------------------ #
    # Read-only market-data delegations (STEP P-B, additive)
    # ------------------------------------------------------------------ #
    def get_depth(self, symbol: str) -> Dict[str, List]:
        """Real order book via the injected read-only data source."""
        return self.data_source.get_depth(symbol)

    def get_market_info(self, symbol: str):
        return self.data_source.get_market_info(symbol)

    def get_fee_for(self, symbol: str) -> Optional[Dict[str, float]]:
        return self.data_source.get_fee_for(symbol)

    def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> List[Dict]:
        return self.data_source.get_candles(symbol, resolution, from_ts, to_ts)

    def get_candles_1m(self, symbol: str, from_ts: int, to_ts: int) -> List[Dict]:
        return self.data_source.get_candles_1m(symbol, from_ts, to_ts)

    def get_candles_1h(self, symbol: str, from_ts: int, to_ts: int) -> List[Dict]:
        return self.data_source.get_candles_1h(symbol, from_ts, to_ts)

    # ------------------------------------------------------------------ #
    # resting-order matching
    # ------------------------------------------------------------------ #
    def check_open_orders(self) -> None:
        """Re-evaluate resting LIMIT orders against the current depth.

        BUY fills while best_ask <= order price; SELL fills while best_bid >=
        order price. Fills partially when depth is thin. No background thread;
        the executor/guardian calls this periodically.
        """
        rows = store_db.get_orders_by_status(list(_OPEN_STATUSES), dry_run=True)
        for row in rows:
            symbol = row.get("symbol")
            side = row.get("side")
            price = float(row.get("price") or 0.0)
            if price <= 0:
                continue
            try:
                depth = self._depth(symbol)
            except Exception:
                continue
            filled = float(row.get("filled_quantity") or 0.0)
            orig = float(row.get("quantity") or 0.0)
            remaining = orig - filled
            if remaining <= 1e-9:
                continue
            if side == "BUY":
                levels = depth.get("asks", [])
                best = levels[0][0] if levels else None
                if best is None or best > price:
                    continue
                match = book.fill_qty(levels, remaining, max_price=price)
            else:
                levels = depth.get("bids", [])
                best = levels[0][0] if levels else None
                if best is None or best < price:
                    continue
                match = book.fill_qty(levels, remaining, min_price=price)
            add_qty = match["filled_qty"]
            if add_qty <= 1e-9:
                continue
            new_filled = filled + add_qty
            avg_price = match["avg_price"] or 0.0
            add_notional = add_qty * avg_price
            fee_rate = self._taker_fee_for(symbol)
            base = base_asset(symbol)
            fee_in_base = (side == "BUY" and self.cfg.trading.paper_buy_fee_in_base)
            if fee_in_base:
                fee_qty = add_qty * fee_rate
                fee_tmn = fee_qty * avg_price
            else:
                fee_tmn = add_notional * fee_rate
            if side == "BUY":
                cost = add_notional if not fee_in_base else add_notional
                # release the locked TMN for the newly filled part
                free_deltas = {"TMN": -cost, base: add_qty - (fee_qty if fee_in_base else 0.0)}
                lock_deltas = {"TMN": -add_qty * price}
            else:
                free_deltas = {base: -add_qty, "TMN": add_notional - fee_tmn}
                lock_deltas = {base: -add_qty}
            try:
                self._apply_deltas(free_deltas, lock_deltas)
            except PaperExchangeError:
                continue
            status = PaperStatus.FILLED if new_filled >= orig - 1e-9 else PaperStatus.PARTIALLY_FILLED
            self._update_order_row(
                row["id"], status=status,
                filled_quantity=new_filled,
                avg_fill_price=avg_price,
                fee_tmn=float(row.get("fee_tmn") or 0.0) + fee_tmn,
            )
