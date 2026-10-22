#!/usr/bin/env python3
"""Live order probe (PART 2). Requires --i-understand-real-money AND DRY_RUN=False."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import time
from typing import List, Optional

import config
from exchange.wallex import WallexClient, WallexError

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "docs" / "samples"
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)


def save_json(prefix: str, data) -> None:
    fname = f"probe_orders_{prefix}_{int(time.time())}.json"
    path = SAMPLE_DIR / fname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  saved -> {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live order probe")
    parser.add_argument("--i-understand-real-money", action="store_true", required=True,
                        help="Confirm you understand this places real orders")
    parser.add_argument("--symbol", default=None,
                        help="Symbol to probe (default first in CONFIG.SYMBOLS)")
    args = parser.parse_args()

    if config.CONFIG.trading.dry_run:
        print("ERROR: DRY_RUN is True in config. This probe requires live mode.")
        sys.exit(1)

    symbol = args.symbol or config.CONFIG.trading.SYMBOLS[0]
    print(f"Probing live orders for {symbol}")

    client = WallexClient(config.CONFIG, db=None)
    statuses: List[str] = []
    min_order_error: Optional[str] = None
    symbol_format_ok = False

    try:
        depth = client.get_depth(symbol)
        if not depth["bids"]:
            print("No bids in book")
            return
        best_bid = depth["bids"][0][0]
        symbol_format_ok = True
        print(f"Best bid: {best_bid}")

        limit_price = round(best_bid * 0.8, 2)
        qty = max(0.001, 50000 / limit_price)
        print(f"Placing LIMIT BUY {qty} @ {limit_price}")

        order_resp = client.create_order(symbol=symbol, side="BUY", type="LIMIT",
                                         quantity=qty, price=limit_price)
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
