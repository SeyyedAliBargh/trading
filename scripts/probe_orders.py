#!/usr/bin/env python3
"""Live order probe (STEP 4d). Requires --i-understand-real-money AND DRY_RUN=False.

Real-probe fixes baked in (see docs/wallex_api.md):
- price/quantity are floored to the market's price_precision/amount_precision
  (the previous run sent 17671321516.8 and got HTTP 422 "price must have 0 decimals");
- the notional is capped at 100,000 TMN (the previous run sized 0.001 BTC ~ 17M TMN
  because of a hardcoded max(0.001, ...) floor);
- the probe ABORTS before sending if the notional is outside [50,000, 100,000] TMN.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import time
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional

import config
from exchange.wallex import WallexClient, WallexError, format_number

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "docs" / "samples"
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

PROBE_TARGET_NOTIONAL_TMN = 100_000.0
PROBE_MIN_NOTIONAL_TMN = 50_000.0
PROBE_MAX_NOTIONAL_TMN = 100_000.0
PROBE_PRICE_DISCOUNT = 0.8


def size_limit_buy(best_bid: float, price_precision: Optional[int],
                   amount_precision: Optional[int],
                   target_notional_tmn: float = PROBE_TARGET_NOTIONAL_TMN,
                   discount: float = PROBE_PRICE_DISCOUNT,
                   min_notional_tmn: float = PROBE_MIN_NOTIONAL_TMN,
                   max_notional_tmn: float = PROBE_MAX_NOTIONAL_TMN) -> dict:
    """Pure sizing for the live order probe (Decimal, ROUND_DOWN; never raises).

    price    = floor(best_bid * discount) at price_precision
    quantity = floor(target_notional / price) at amount_precision
    notional = price * quantity (exact Decimal)

    Returns dict(price, quantity, notional, price_str, quantity_str, ok, reason).
    ok=False with a reason when the price/qty floors to zero or the notional
    is outside [min_notional_tmn, max_notional_tmn].
    """
    reason: Optional[str] = None
    try:
        if best_bid is None or best_bid <= 0:
            return {"price": None, "quantity": None, "notional": None,
                    "price_str": None, "quantity_str": None,
                    "ok": False, "reason": "best_bid must be positive"}
        pp = int(price_precision) if price_precision is not None else 0
        ap = int(amount_precision) if amount_precision is not None else 8
        price = (Decimal(str(best_bid)) * Decimal(str(discount))).quantize(
            Decimal(1).scaleb(-pp), rounding=ROUND_DOWN)
        if price <= 0:
            reason = f"floored price {price} <= 0 at price_precision {pp}"
            quantity, notional = Decimal(0), Decimal(0)
        else:
            quantity = (Decimal(str(target_notional_tmn)) / price).quantize(
                Decimal(1).scaleb(-ap), rounding=ROUND_DOWN)
            notional = price * quantity
        if price > 0 and quantity <= 0:
            reason = f"quantity floors to {quantity} at amount_precision {ap}"
        elif reason is None:
            if notional < Decimal(str(min_notional_tmn)):
                reason = f"notional {notional} < min {min_notional_tmn} TMN"
            elif notional > Decimal(str(max_notional_tmn)):
                reason = f"notional {notional} > max {max_notional_tmn} TMN"
        return {
            "price": float(price),
            "quantity": float(quantity),
            "notional": float(notional),
            "price_str": format_number(float(price), pp),
            "quantity_str": format_number(float(quantity), ap),
            "ok": reason is None,
            "reason": reason,
        }
    except Exception as exc:  # never raise: sizing is a decision input
        return {"price": None, "quantity": None, "notional": None,
                "price_str": None, "quantity_str": None,
                "ok": False, "reason": f"sizing error: {exc}"}


def save_json(prefix: str, data) -> None:
    fname = f"probe_orders_{prefix}_{int(time.time())}.json"
    path = SAMPLE_DIR / fname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  saved -> {path}")


def pick_default_symbol(client: WallexClient, preferred: Optional[str] = None) -> Optional[str]:
    """The configured probe default if it is tradeable; otherwise the
    CONFIG.SYMBOLS symbol with the lowest price per unit (cheapest to size a
    ~100k TMN order)."""
    symbols = list(config.CONFIG.trading.SYMBOLS)
    if not symbols:
        return None
    if preferred is None:
        preferred = config.CONFIG.trading.probe_default_symbol
    if preferred and preferred in symbols:
        return preferred
    best: Optional[tuple] = None  # (best_bid, symbol)
    for sym in symbols:
        try:
            depth = client.get_depth(sym)
            bb = depth["bids"][0][0] if depth["bids"] else None
            if bb and (best is None or bb < best[0]):
                best = (bb, sym)
        except Exception as exc:
            print(f"  depth failed for {sym}: {exc}")
        time.sleep(0.3)
    return best[1] if best else symbols[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Live order probe")
    parser.add_argument("--i-understand-real-money", action="store_true", required=True,
                        help="Confirm you understand this places real orders")
    parser.add_argument("--symbol", default=None,
                        help="Symbol to probe (default: the configured probe "
                             "default if tradeable, else the cheapest CONFIG.SYMBOLS entry)")
    args = parser.parse_args()

    if config.CONFIG.trading.dry_run:
        print("ERROR: DRY_RUN is True in config. This probe requires live mode.")
        sys.exit(1)

    client = WallexClient(config.CONFIG, db=None)
    symbol = args.symbol or pick_default_symbol(client)
    if not symbol:
        print("ERROR: no symbol to probe (CONFIG.SYMBOLS is empty).")
        sys.exit(1)
    print(f"Probing live orders for {symbol}")

    statuses: List[str] = []
    min_order_error: Optional[str] = None
    symbol_format_ok = False

    try:
        market = client.get_market_info(symbol)
        if market is None:
            print(f"ABORT: {symbol} not found in markets (cannot size safely).")
            sys.exit(1)
        print(f"Market info: price_precision={market.price_precision} "
              f"amount_precision={market.amount_precision}")

        depth = client.get_depth(symbol)
        if not depth["bids"]:
            print("No bids in book")
            return
        best_bid = depth["bids"][0][0]
        symbol_format_ok = True
        print(f"Best bid: {best_bid}")

        sizing = size_limit_buy(best_bid, market.price_precision, market.amount_precision)
        print(f"Sizing: price={sizing['price_str']} quantity={sizing['quantity_str']} "
              f"notional={sizing['notional']} TMN")
        if not sizing["ok"]:
            print(f"ABORT: {sizing['reason']}")
            print("No order was sent.")
            save_json("abort", {"symbol": symbol, "best_bid": best_bid, "sizing": sizing})
            return

        print(f"Placing LIMIT BUY {sizing['quantity_str']} @ {sizing['price_str']} "
              f"(notional {sizing['notional']} TMN)")
        order_resp = client.create_order(symbol=symbol, side="BUY", type="LIMIT",
                                         quantity=sizing["quantity"],
                                         price=sizing["price"])
        save_json("create", order_resp)
        client_id = order_resp["order"].get("clientOrderId")
        print(f"Order client_id: {client_id}")

        status_resp = client.get_order(client_id)
        save_json("status_after_create", status_resp)
        s1 = status_resp["order"].get("status")
        print(f"Status after create: {s1}, active={status_resp['order'].get('active')}")
        if s1:
            statuses.append(s1)

        open_resp = client.get_open_orders(symbol)
        save_json("open_orders_1", open_resp)
        print(f"Open orders count: {len(open_resp['orders'])}")

        cancel_resp = client.cancel_order(client_id)
        save_json("cancel", cancel_resp)
        s2 = cancel_resp["order"].get("status")
        print(f"Cancel status: {s2}, active={cancel_resp['order'].get('active')}")
        if s2:
            statuses.append(s2)

        status_resp2 = client.get_order(client_id)
        save_json("status_after_cancel", status_resp2)
        s3 = status_resp2["order"].get("status")
        print(f"Status after cancel: {s3}, active={status_resp2['order'].get('active')}")
        if s3:
            statuses.append(s3)

        open_resp2 = client.get_open_orders(symbol)
        save_json("open_orders_2", open_resp2)
        print(f"Open orders count after cancel: {len(open_resp2['orders'])}")

    except WallexError as e:
        print(f"WallexError: {e}")
        min_order_error = str(e)
        save_json("error", {"error": str(e)})
    except Exception as e:
        print(f"Unexpected error: {e}")
        save_json("error", {"error": str(e)})
    finally:
        print("\n=== FACTS LEARNED ===")
        print(f"Distinct status strings seen: {sorted(set(statuses)) if statuses else 'none'}")
        print(f"Min-order error text: {min_order_error if min_order_error else 'none'}")
        print(f"Symbol format worked: {symbol_format_ok}")
        print("Probe finished.")


if __name__ == "__main__":
    main()
