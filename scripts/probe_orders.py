#!/usr/bin/env python3
"""Live order probe (PART 2). Requires --i-understand-real-money and DRY_RUN=False."""
import sys
import argparse
import json
from pathlib import Path
import config
from exchange.wallex import WallexClient, WallexError

SAMPLE_DIR = Path("docs/samples")
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)


def save_json(prefix: str, data):
    import time
    fname = f"probe_orders_{prefix}_{int(time.time())}.json"
    path = SAMPLE_DIR / fname
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  saved -> {path}")


def main():
    parser = argparse.ArgumentParser(description="Live order probe")
    parser.add_argument("--i-understand-real-money", action="store_true", required=True,
                        help="Confirm you understand this places real orders")
    parser.add_argument("--symbol", default=None, help="Symbol to probe (default first in CONFIG.SYMBOLS)")
    args = parser.parse_args()

    if config.CONFIG.trading.dry_run:
        print("ERROR: DRY_RUN is True in config. This probe requires live mode.")
        sys.exit(1)

    symbol = args.symbol or config.CONFIG.trading.SYMBOLS[0]
    print(f"Probing live orders for {symbol}")

    client = WallexClient(config.CONFIG, db=None)

    try:
        status_strings = []
        min_order_error = None
        symbol_format_ok = False

        # get best bid
        depth = client.get_depth(symbol)
        if not depth["bids"]:
            print("No bids in book")
            return
        best_bid = depth["bids"][0][0]
        symbol_format_ok = True
        print(f"Best bid: {best_bid}")

        # place LIMIT BUY ~20% below best bid
        limit_price = round(best_bid * 0.8, 2)
        qty = max(0.001, 50000 / limit_price)
        print(f"Placing LIMIT BUY {qty} @ {limit_price}")

        order_resp = client.create_order(symbol=symbol, side="BUY", type="LIMIT",
                                         quantity=qty, price=limit_price)
        save_json("create", order_resp)
        client_id = order_resp["order"].get("client_id") or order_resp["order"].get("clientOrderId")
        print(f"Order client_id: {client_id}")

        status_resp = client.get_order(client_id)
        save_json("status_after_create", status_resp)
        status1 = status_resp['order'].get('status')
        active1 = status_resp['order'].get('active')
        print(f"Status after create: {status1}, active: {active1}")
        if status1:
            status_strings.append(status1)

        open_resp = client.get_open_orders(symbol)
        save_json("open_orders_1", open_resp)
        print(f"Open orders count: {len(open_resp['orders'])}")

        cancel_resp = client.cancel_order(client_id)
        save_json("cancel", cancel_resp)
        cancel_status = cancel_resp['order'].get('status')
        cancel_active = cancel_resp['order'].get('active')
        print(f"Cancel status: {cancel_status}, active: {cancel_active}")
        if cancel_status:
            status_strings.append(cancel_status)

        status_resp2 = client.get_order(client_id)
        save_json("status_after_cancel", status_resp2)
        final_status = status_resp2['order'].get('status')
        final_active = status_resp2['order'].get('active')
        print(f"Status after cancel: {final_status}, active: {final_active}")
        if final_status:
            status_strings.append(final_status)

    except WallexError as e:
        print(f"WallexError: {e}")
        min_order_error = str(e)
        save_json("error", {"error": str(e)})
    except Exception as e:
        print(f"Unexpected error: {e}")
        save_json("error", {"error": str(e)})
    finally:
        print("\n=== FACTS LEARNED ===")
        print(f"Distinct status strings seen: {sorted(set(status_strings)) if status_strings else 'none'}")
        print(f"Min-order error text: {min_order_error if min_order_error else 'none'}")
        print(f"Symbol format worked: {symbol_format_ok}")
        # HTTP codes not directly captured; could be inferred from last_headers if needed
        print("Probe finished.")


if __name__ == "__main__":
    main()