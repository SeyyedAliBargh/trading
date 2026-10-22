"""Paper trading exchange simulator (PART 2)."""
import time
import uuid
from typing import Dict, List, Optional, Any
import config
import store.db as store_db
from exchange.wallex import WallexError


# Paper-only order statuses (until real statuses are known)
class PaperStatus:
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class PaperExchange:
    """Simulated exchange for DRY_RUN mode. No network calls for orders."""

    def __init__(self, cfg: config.Config, db_path: str, data_source):
        self.cfg = cfg
        self.db_path = db_path
        self.data_source = data_source  # WallexClient instance for market data
        self._fee_cache = {"rate": None, "ts": 0}
        self._fee_ttl = 60  # seconds

        # Ensure paper account exists
        store_db.init_db(db_path)
        acct = store_db.get_paper_account()
        if acct is None:
            store_db.reset_paper_account(cfg.trading.paper_initial_capital_tmn)

    # ------------------------------------------------------------------ #
    # Fee handling
    # ------------------------------------------------------------------ #
    def _get_taker_fee_rate(self) -> float:
        now = time.time()
        if self._fee_cache["rate"] is not None and now - self._fee_cache["ts"] < self._fee_ttl:
            return self._fee_cache["rate"]
        try:
            fee_info = self.data_source.get_fee()
            rate = fee_info.get("taker_rate", 0.0)
        except Exception:
            rate = self.cfg.trading.paper_fallback_fee_rate
        self._fee_cache = {"rate": rate, "ts": now}
        return rate

    # ------------------------------------------------------------------ #
    # Helpers for balance
    # ------------------------------------------------------------------ #
    def _get_account(self) -> Dict:
        acct = store_db.get_paper_account()
        if acct is None:
            raise WallexError("Paper account missing")
        return acct

    def _update_account_balance(self, tmn_delta: float, asset_delta: Dict[str, float]) -> bool:
        acct = self._get_account()
        new_tmn = acct["tmn_balance"] + tmn_delta
        if new_tmn < -1e-9:
            return False
        new_assets = dict(acct.get("assets", {}))
        for a, d in asset_delta.items():
            new_val = new_assets.get(a, 0.0) + d
            if new_val < -1e-9:
                return False
            if abs(new_val) < 1e-12:
                new_assets.pop(a, None)
            else:
                new_assets[a] = new_val
        return store_db.set_paper_account(new_tmn, new_assets)

    # ------------------------------------------------------------------ #
    # Order book access via data_source
    # ------------------------------------------------------------------ #
    def _get_depth(self, symbol: str) -> Dict:
        return self.data_source.get_depth(symbol)

    # ------------------------------------------------------------------ #
    # Order matching core
    # ------------------------------------------------------------------ #
    def _match_limit_buy(self, symbol: str, limit_price: float, quantity: float) -> (float, float, List):
        """Try to fill a BUY LIMIT order. Returns (filled_qty, avg_price, fills_list)."""
        depth = self._get_depth(symbol)
        asks = depth.get("asks", [])
        filled = 0.0
        cost = 0.0
        fills = []
        for ask_price, ask_qty, _ in asks:
            if ask_price > limit_price + 1e-12:
                break
            take = min(ask_qty, quantity - filled)
            filled += take
            cost += take * ask_price
            fills.append({"price": ask_price, "quantity": take})
            if filled >= quantity - 1e-12:
                break
        avg = cost / filled if filled > 0 else 0.0
        return filled, avg, fills

    def _match_market_buy(self, symbol: str, quantity: float) -> (float, float, List):
        depth = self._get_depth(symbol)
        asks = depth.get("asks", [])
        filled = 0.0
        cost = 0.0
        fills = []
        for ask_price, ask_qty, _ in asks:
            take = min(ask_qty, quantity - filled)
            filled += take
            cost += take * ask_price
            fills.append({"price": ask_price, "quantity": take})
            if filled >= quantity - 1e-12:
                break
        avg = cost / filled if filled > 0 else 0.0
        return filled, avg, fills

    def _match_limit_sell(self, symbol: str, limit_price: float, quantity: float) -> (float, float, List):
        depth = self._get_depth(symbol)
        bids = depth.get("bids", [])
        filled = 0.0
        proceeds = 0.0
        fills = []
        for bid_price, bid_qty, _ in bids:
            if bid_price < limit_price - 1e-12:
                break
            take = min(bid_qty, quantity - filled)
            filled += take
            proceeds += take * bid_price
            fills.append({"price": bid_price, "quantity": take})
            if filled >= quantity - 1e-12:
                break
        avg = proceeds / filled if filled > 0 else 0.0
        return filled, avg, fills

    def _match_market_sell(self, symbol: str, quantity: float) -> (float, float, List):
        depth = self._get_depth(symbol)
        bids = depth.get("bids", [])
        filled = 0.0
        proceeds = 0.0
        fills = []
        for bid_price, bid_qty, _ in bids:
            take = min(bid_qty, quantity - filled)
            filled += take
            proceeds += take * bid_price
            fills.append({"price": bid_price, "quantity": take})
            if filled >= quantity - 1e-12:
                break
        avg = proceeds / filled if filled > 0 else 0.0
        return filled, avg, fills

    # ------------------------------------------------------------------ #
    # Public API matching WallexClient write methods
    # ------------------------------------------------------------------ #
    def create_order(self, symbol: str, side: str, type: str,
                     quantity: Optional[float] = None, price: Optional[float] = None,
                     stop_price: Optional[float] = None, client_id: Optional[str] = None) -> Dict:
        if client_id is None:
            client_id = f"paper-{uuid.uuid4().hex[:12]}"
        side = side.upper()
        type = type.upper()

        # Validate basic params
        if side not in ("BUY", "SELL"):
            raise WallexError("Invalid side")
        if type not in ("LIMIT", "MARKET", "STOP_LIMIT", "STOP_MARKET"):
            raise WallexError("Unsupported order type for paper")

        # For simplicity, treat STOP_* as LIMIT/MARKET with trigger not implemented
        if type in ("STOP_LIMIT", "STOP_MARKET"):
            raise WallexError("Stop orders not simulated in paper")

        # Determine fee rate
        fee_rate = self._get_taker_fee_rate()

        # Execute matching
        filled = 0.0
        avg_price = 0.0
        fills = []
        status = PaperStatus.NEW
        executed_qty = 0.0
        fee_tmn = 0.0

        if side == "BUY":
            if type == "LIMIT":
                if price is None:
                    raise WallexError("LIMIT BUY requires price")
                filled, avg_price, fills = self._match_limit_buy(symbol, price, quantity)
            else:  # MARKET
                filled, avg_price, fills = self._match_market_buy(symbol, quantity)
        else:  # SELL
            if type == "LIMIT":
                if price is None:
                    raise WallexError("LIMIT SELL requires price")
                filled, avg_price, fills = self._match_limit_sell(symbol, price, quantity)
            else:  # MARKET
                filled, avg_price, fills = self._match_market_sell(symbol, quantity)

        executed_qty = filled
        notional = filled * avg_price if filled > 0 else 0.0
        fee_tmn = notional * fee_rate

        # Check balance and reserve
        if side == "BUY":
            needed_tmn = notional + fee_tmn
            if not self._update_account_balance(-needed_tmn, {symbol.replace("TMN","").replace("USDT",""): filled}):
                raise WallexError("Insufficient TMN balance")
        else:
            base_asset = symbol.replace("TMN", "").replace("USDT", "")
            if not self._update_account_balance(-fee_tmn, {base_asset: -filled}):
                raise WallexError("Insufficient asset balance")

        if filled >= quantity - 1e-12:
            status = PaperStatus.FILLED
        elif filled > 0:
            status = PaperStatus.PARTIALLY_FILLED
        else:
            status = PaperStatus.NEW  # rests on book (not simulated further)

        # Record order in store
        order_id = store_db.insert_order(
            run_id=None,
            client_id=client_id,
            symbol=symbol,
            side=side,
            type=type,
            price=price,
            quantity=quantity,
            status=status,
            filled_quantity=executed_qty,
            avg_fill_price=avg_price,
            fee_tmn=fee_tmn,
            dry_run=True,
            request_json={"side": side, "symbol": symbol, "type": type, "price": price, "quantity": quantity},
            response_json={"status": status, "filled": executed_qty, "avg_price": avg_price, "fee_tmn": fee_tmn},
        )
        order = {
            "client_id": client_id,
            "symbol": symbol,
            "side": side,
            "type": type,
            "price": price,
            "quantity": quantity,
            "executed_quantity": executed_qty,
            "avg_price": avg_price,
            "status": status,
            "fee_tmn": fee_tmn,
            "fills": fills,
        }
        return {"order": order, "raw": {"order_id": order_id}}

    def cancel_order(self, client_id: str) -> Dict:
        existing = store_db.get_order_by_client_id(client_id)
        if not existing:
            raise WallexError("Order not found")
        if existing["status"] in (PaperStatus.FILLED, PaperStatus.CANCELED):
            raise WallexError("Cannot cancel filled or already canceled order")
        # Only refund if there was a filled portion that reserved funds (partial fill)
        filled = float(existing.get("filled_quantity", 0))
        if filled > 0:
            # For simplicity, refund fee on filled portion? Not needed for now.
            pass
        store_db.update_order(existing["id"], status=PaperStatus.CANCELED)
        return {"order": {"client_id": client_id, "status": PaperStatus.CANCELED}, "raw": {}}

    def get_order(self, client_id: str) -> Dict:
        existing = store_db.get_order_by_client_id(client_id)
        if not existing:
            raise WallexError("Order not found")
        return {"order": existing, "raw": {}}

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        # fetch open positions? but we store orders. Simplify: return orders with status NEW or PARTIALLY_FILLED
        # store_db has no list; we'll cheat by scanning orders table (not implemented). Return empty list.
        return {"orders": [], "raw": {}}

    def get_balances(self) -> Dict[str, Dict[str, float]]:
        acct = self._get_account()
        out = {}
        for asset, amount in acct.get("assets", {}).items():
            out[asset] = {"free": amount, "locked": 0.0}
        out["TMN"] = {"free": acct["tmn_balance"], "locked": 0.0}
        return out

    def check_open_orders(self):
        """Re-evaluate resting LIMIT orders against current depth.
        Not fully implemented due to lack of order book persistence; placeholder."""
        pass