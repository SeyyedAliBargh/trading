"""Wallex REST client (read + write)."""
import time
import json
import re
import requests
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple, Union
import config
import store.db as store_db


class WallexError(Exception):
    """Wallex API error. Never contains the API key."""
    def __init__(self, message: str, endpoint: str = "", http_status: Optional[int] = None,
                 code: Optional[int] = None, retryable: bool = False):
        super().__init__(message)
        self.endpoint = endpoint
        self.http_status = http_status
        self.code = code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# client_id helpers
# ---------------------------------------------------------------------------
_CLIENT_ID_RE = re.compile(r'^[A-Za-z0-9_]+$')


def _validate_client_id(cid: str) -> None:
    if not _CLIENT_ID_RE.fullmatch(cid):
        raise ValueError(f"Invalid client_id '{cid}': only A-Z a-z 0-9 _ allowed")


def _generate_client_id(symbol: str, counter: int) -> str:
    run_part = str(int(time.time()))
    cid = f"ag_{run_part}_{symbol}_{counter}"
    _validate_client_id(cid)
    return cid


# ---------------------------------------------------------------------------
# numeric helpers
# ---------------------------------------------------------------------------
def _format_number(value: float, precision: Optional[int]) -> str:
    if precision is None:
        return ("{:f}".format(value)).rstrip('0').rstrip('.')
    d = Decimal(str(value))
    quant = Decimal('1').scaleb(-precision)
    d = d.quantize(quant, rounding=ROUND_DOWN)
    return f"{d:.{precision}f}"


def _parse_number(val: Union[str, int, float, None]) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# base/quote asset helpers
# ---------------------------------------------------------------------------
def base_asset(symbol: str, quote_suffixes: Tuple[str, ...] = ("TMN", "USDT")) -> str:
    """Return the base asset of a symbol like 'BTCTMN' -> 'BTC'.
    Strips the FIRST matching quote suffix from the END (longest first)."""
    for q in sorted(quote_suffixes, key=len, reverse=True):
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[:-len(q)]
    return symbol


# ---------------------------------------------------------------------------
# Market info dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class MarketInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    price_precision: Optional[int]
    amount_precision: Optional[int]
    is_market_type_enable: Optional[bool]
    is_zero_fee: Optional[bool]
    flags: List[str] = field(default_factory=list)
    quote_volume_24h: Optional[float] = None


# ---------------------------------------------------------------------------
# order parser (shared by all order endpoints)
# ---------------------------------------------------------------------------
def _parse_order(raw: Dict) -> Dict:
    def g(key, default=None):
        return raw.get(key, default)
    return {
        "active": g("active"),
        "clientOrderId": g("clientOrderId"),
        "created_at": g("created_at"),
        "executedPercent": _parse_number(g("executedPercent")),
        "executedPrice": _parse_number(g("executedPrice")),
        "executedQty": _parse_number(g("executedQty")),
        "executedSum": _parse_number(g("executedSum")),
        "fee": _parse_number(g("fee")),
        "fills": g("fills", []),
        "origQty": _parse_number(g("origQty")),
        "price": _parse_number(g("price")),
        "side": g("side"),
        "status": g("status"),
        "stopPrice": _parse_number(g("stopPrice")),
        "stopPriceCondition": g("stopPriceCondition"),
        "sum": _parse_number(g("sum")),
        "symbol": g("symbol"),
        "transactTime": _parse_number(g("transactTime")),
        "type": g("type"),
        "updated_at": g("updated_at"),
    }


# ---------------------------------------------------------------------------
# WallexClient
# ---------------------------------------------------------------------------
class WallexClient:
    """Synchronous Wallex REST client."""

    FEE_TTL_SEC = 60
    MARKETS_TTL_SEC = 300

    def __init__(self, cfg: config.Config, db=None, markets_map: Optional[Dict[str, Dict]] = None):
        self.cfg = cfg
        self.db = db
        self.session = requests.Session()
        self.session.headers.update({
            "x-api-key": cfg.exchange.api_key or "",
            "Accept": "application/json",
        })
        self.base_url = cfg.exchange.WALLEX_BASE_URL.rstrip("/")
        self.timeout = cfg.exchange.request_timeout_sec
        self.last_headers: Dict[str, str] = {}
        self._markets_map = markets_map or {}
        self._order_counter = 0
        # caches: {"ts": float, "data": ...}
        self._fee_cache: Optional[Dict[str, Any]] = None
        self._markets_cache: Optional[Dict[str, Any]] = None

    # -- internal request ------------------------------------------------
    def _request(self, method: str, path: str, params: Optional[Dict] = None,
                 json_body: Optional[Dict] = None) -> Any:
        url = f"{self.base_url}{path}"
        start = time.perf_counter()
        http_status = None
        try:
            resp = self.session.request(method, url, params=params, json=json_body, timeout=self.timeout)
            http_status = resp.status_code
            self.last_headers = dict(resp.headers)
            latency_ms = (time.perf_counter() - start) * 1000

            if self.db is not None:
                try:
                    store_db.insert_api_call(
                        run_id=None, service="wallex", method=method, path=path,
                        request_json={"params": params, "json": json_body},
                        status_code=http_status, response_text=resp.text[:500],
                        latency_ms=latency_ms, error=None,
                    )
                except Exception:
                    pass

            if http_status >= 400:
                retryable = http_status in (429, 500, 502, 503, 504)
                raise WallexError(f"HTTP {http_status}: {resp.text[:200]}",
                                  endpoint=path, http_status=http_status, retryable=retryable)

            try:
                data = resp.json()
            except json.JSONDecodeError as e:
                raise WallexError(f"Invalid JSON: {e}", endpoint=path, http_status=http_status, retryable=False)

            if isinstance(data, dict) and data.get("success") is False:
                code = data.get("code")
                msg = data.get("message") or data.get("result") or "API error"
                raise WallexError(str(msg), endpoint=path, http_status=http_status, code=code, retryable=False)

            return data

        except requests.exceptions.Timeout as e:
            latency_ms = (time.perf_counter() - start) * 1000
            self._log_error(method, path, params, json_body, latency_ms, str(e))
            raise WallexError(f"Timeout: {e}", endpoint=path, http_status=None, retryable=True)
        except requests.exceptions.RequestException as e:
            latency_ms = (time.perf_counter() - start) * 1000
            self._log_error(method, path, params, json_body, latency_ms, str(e))
            raise WallexError(f"Network error: {e}", endpoint=path, http_status=None, retryable=True)

    def _log_error(self, method, path, params, json_body, latency_ms, err):
        if self.db is not None:
            try:
                store_db.insert_api_call(run_id=None, service="wallex", method=method, path=path,
                                         request_json={"params": params, "json": json_body},
                                         status_code=None, response_text="", latency_ms=latency_ms, error=err)
            except Exception:
                pass

    # -- live interlock ---------------------------------------------------
    def _check_live(self):
        if self.cfg.trading.dry_run:
            raise WallexError("Write endpoint called in DRY_RUN mode", endpoint="", retryable=False)

    # -- precision helpers ------------------------------------------------
    def _get_precision(self, symbol: str) -> Tuple[Optional[int], Optional[int]]:
        m = self._markets_map.get(symbol)
        if m:
            return m.get("price_precision"), m.get("amount_precision")
        return None, None

    def _format_price_qty(self, symbol: str, price: Optional[float], quantity: Optional[float]):
        pp, ap = self._get_precision(symbol)
        price_str = _format_number(price, pp) if price is not None else None
        qty_str = _format_number(quantity, ap) if quantity is not None else None
        return price_str, qty_str

    # -- read-only: markets ------------------------------------------------
    def get_markets(self) -> List[Dict]:
        data = self._request("GET", "/hector/web/v1/markets")
        try:
            markets = data["result"]["markets"]
            # refresh markets cache
            self._markets_cache = {"ts": time.time(), "data": markets}
            # also refresh the precision map
            self._markets_map = {m.get("symbol", ""): m for m in markets if isinstance(m, dict)}
            return markets
        except (KeyError, TypeError) as e:
            raise WallexError(f"Unexpected markets response: {e}", endpoint="/hector/web/v1/markets")

    def _get_markets_cached(self) -> List[Dict]:
        if (self._markets_cache is not None
                and time.time() - self._markets_cache["ts"] < self.MARKETS_TTL_SEC):
            return self._markets_cache["data"]
        return self.get_markets()

    def get_market_info(self, symbol: str) -> Optional[MarketInfo]:
        """Return MarketInfo for the symbol (from cached markets, TTL 300s)."""
        markets = self._get_markets_cached()
        for m in markets:
            if not isinstance(m, dict):
                continue
            if m.get("symbol") != symbol:
                continue
            return MarketInfo(
                symbol=symbol,
                base_asset=m.get("base_asset") or base_asset(symbol),
                quote_asset=m.get("quote_asset") or ("TMN" if symbol.endswith("TMN") else "USDT"),
                price_precision=m.get("price_precision"),
                amount_precision=m.get("amount_precision"),
                is_market_type_enable=m.get("is_market_type_enable"),
                is_zero_fee=m.get("is_zero_fee"),
                flags=m.get("flags", []),
                quote_volume_24h=_parse_number(m.get("quote_volume_24h")),
            )
        return None

    # -- read-only: depth --------------------------------------------------
    def get_depth(self, symbol: str) -> Dict[str, List[tuple]]:
        data = self._request("GET", "/v1/depth", params={"symbol": symbol})
        try:
            res = data["result"]
            bids = [(float(b["price"]), float(b["quantity"]), float(b["sum"])) for b in res.get("bid", [])]
            asks = [(float(a["price"]), float(a["quantity"]), float(a["sum"])) for a in res.get("ask", [])]
            bids.sort(key=lambda x: x[0], reverse=True)
            asks.sort(key=lambda x: x[0])
            return {"bids": bids, "asks": asks}
        except (KeyError, TypeError, ValueError) as e:
            raise WallexError(f"Unexpected depth response: {e}", endpoint="/v1/depth")

    # -- read-only: candles ------------------------------------------------
    def get_candles(self, symbol: str, resolution: str, from_ts: int, to_ts: int) -> List[Dict]:
        """GET /v1/udf/history. resolution is a string like '1','5','15','60','D'.
        from_ts/to_ts are epoch seconds."""
        params = {"symbol": symbol, "resolution": str(resolution), "from": int(from_ts), "to": int(to_ts)}
        data = self._request("GET", "/v1/udf/history", params=params)
        status = data.get("s")
        if status == "ok":
            try:
                return [{
                    "t": int(t), "o": float(o), "h": float(h), "l": float(l),
                    "c": float(c), "v": float(v)
                } for t, o, h, l, c, v in zip(data["t"], data["o"], data["h"],
                                               data["l"], data["c"], data["v"])]
            except (KeyError, TypeError, ValueError) as e:
                raise WallexError(f"Malformed candle data: {e}", endpoint="/v1/udf/history")
        elif status == "no_data":
            return []
        else:
            raise WallexError(f"Candle API error: {data.get('errmsg') or status}",
                              endpoint="/v1/udf/history")

    # -- read-only: balances ----------------------------------------------
    def get_balances(self) -> Dict[str, Dict[str, float]]:
        data = self._request("GET", "/v1/account/balances")
        try:
            balances = data["result"]["balances"]
            return {asset: {"free": float(info.get("value", 0)),
                            "locked": float(info.get("locked", 0))}
                    for asset, info in balances.items()}
        except (KeyError, TypeError, ValueError) as e:
            raise WallexError(f"Unexpected balances response: {e}", endpoint="/v1/account/balances")

    # -- read-only: fees ----------------------------------------------------
    def get_fee(self) -> Dict[str, Dict[str, float]]:
        """GET /v1/account/fee -> {symbol: {"maker_rate": float, "taker_rate": float}}.
        Parses ALL markets in the response; values may be strings."""
        data = self._request("GET", "/v1/account/fee")
        try:
            result = data["result"]
            out: Dict[str, Dict[str, float]] = {}
            for symbol, rates in result.items():
                if not isinstance(rates, dict):
                    continue
                out[symbol] = {
                    "maker_rate": _parse_number(rates.get("makerFeeRate")) or 0.0,
                    "taker_rate": _parse_number(rates.get("takerFeeRate")) or 0.0,
                }
            self._fee_cache = {"ts": time.time(), "data": out}
            return out
        except (KeyError, TypeError, ValueError) as e:
            raise WallexError(f"Unexpected fee response: {e}", endpoint="/v1/account/fee")

    def get_fee_for(self, symbol: str) -> Optional[Dict[str, float]]:
        """Return {"maker_rate": float, "taker_rate": float} for one symbol, or None."""
        if (self._fee_cache is not None
                and time.time() - self._fee_cache["ts"] < self.FEE_TTL_SEC):
            fees = self._fee_cache["data"]
        else:
            try:
                fees = self.get_fee()
            except WallexError:
                return None
        return fees.get(symbol)

    # -- write endpoints ----------------------------------------------------
    def _next_client_id(self, symbol: str) -> str:
        self._order_counter += 1
        return _generate_client_id(symbol, self._order_counter)

    def create_order(self, symbol: str, side: str, type: str,
                     quantity: Optional[float] = None, price: Optional[float] = None,
                     stop_price: Optional[float] = None,
                     client_id: Optional[str] = None) -> Dict:
        """POST /v1/account/orders (201 on success)."""
        self._check_live()
        if client_id is None:
            client_id = self._next_client_id(symbol)
        _validate_client_id(client_id)

        side_u = side.upper()
        type_u = type.upper()
        price_str, qty_str = self._format_price_qty(symbol, price, quantity)

        body = {"side": side_u, "symbol": symbol, "type": type_u}
        # MARKET orders: send quantity, omit price (may change if API demands price)
        if type_u == "MARKET":
            if quantity is None:
                raise ValueError("MARKET order requires quantity")
            body["quantity"] = qty_str
        else:
            if quantity is None:
                raise ValueError(f"{type_u} order requires quantity")
            body["quantity"] = qty_str
            if price is not None:
                body["price"] = price_str
        if type_u in ("STOP_LIMIT", "STOP_MARKET"):
            if stop_price is None:
                raise ValueError(f"{type_u} requires stop_price")
            body["stop_Price"] = _format_number(stop_price, self._get_precision(symbol)[0])
        body["client_id"] = client_id

        data = self._request("POST", "/v1/account/orders", json_body=body)
        try:
            order = _parse_order(data["result"])
            return {"order": order, "raw": data}
        except (KeyError, TypeError) as e:
            raise WallexError(f"Unexpected create_order response: {e}", endpoint="/v1/account/orders")

    def cancel_order(self, client_id: str) -> Dict:
        """DELETE /v1/account/orders/{client_id} (no body)."""
        self._check_live()
        data = self._request("DELETE", f"/v1/account/orders/{client_id}")
        try:
            order = _parse_order(data["result"])
            return {"order": order, "raw": data}
        except (KeyError, TypeError) as e:
            raise WallexError(f"Unexpected cancel_order response: {e}",
                              endpoint=f"/v1/account/orders/{client_id}")

    def get_order(self, client_id: str) -> Dict:
        self._check_live()
        data = self._request("GET", f"/v1/account/orders/{client_id}")
        try:
            order = _parse_order(data["result"])
            return {"order": order, "raw": data}
        except (KeyError, TypeError) as e:
            raise WallexError(f"Unexpected get_order response: {e}",
                              endpoint=f"/v1/account/orders/{client_id}")

    def get_open_orders(self, symbol: Optional[str] = None, page: int = 1,
                        per_page: int = 25) -> Dict:
        self._check_live()
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if symbol:
            params["symbol"] = symbol
        data = self._request("GET", "/v1/account/openOrders", params=params)
        return {"orders": self._flatten_orders(data), "raw": data}

    def get_orders(self, market: Optional[str] = None, page: int = 1, per_page: int = 25,
                   from_date: Optional[str] = None, to_date: Optional[str] = None,
                   side: Optional[str] = None, type_: Optional[str] = None) -> Dict:
        self._check_live()
        params: Dict[str, Any] = {"page": page, "per_page": per_page}
        if market:
            params["market"] = market
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        if side:
            params["side"] = side.upper()
        if type_:
            params["type"] = type_.upper()
        data = self._request("GET", "/v1/account/orders", params=params)
        return {"orders": self._flatten_orders(data), "raw": data}

    @staticmethod
    def _flatten_orders(data: Dict) -> List[Dict]:
        """Flatten a dict-of-lists result (or a plain list) into parsed orders."""
        result = data.get("result", {}) if isinstance(data, dict) else {}
        orders: List[Dict] = []
        if isinstance(result, dict):
            for lst in result.values():
                if isinstance(lst, list):
                    orders.extend(_parse_order(o) for o in lst if isinstance(o, dict))
        elif isinstance(result, list):
            orders = [_parse_order(o) for o in result if isinstance(o, dict)]
        return orders
