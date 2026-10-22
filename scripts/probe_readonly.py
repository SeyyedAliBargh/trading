#!/usr/bin/env python3
"""Read-only probe for Wallex API (PART 1)."""
import sys
import time
import json
import argparse
from pathlib import Path

import config
from exchange.wallex import WallexClient, WallexError

SAMPLE_DIR = Path("docs/samples")
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

def save_json(filename: str, data):
    path = SAMPLE_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  saved -> {path}")

def main():
    print("=== Wallex Read-Only Probe ===")
    print(f"Base URL: {config.CONFIG.exchange.WALLEX_BASE_URL}")
    print(f"Symbols: {config.CONFIG.trading.SYMBOLS}")
    print(f"Dry run (no DB): True")
    print()

    client = WallexClient(config.CONFIG, db=None)
    latencies = []

    def call_with_latency(fn, *args, **kwargs):
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            lat = (time.perf_counter() - start) * 1000
            latencies.append(lat)

    # ---- a) get_markets ----
    print("--- a) get_markets ---")
    try:
        markets = call_with_latency(client.get_markets)
        print(f"Total markets: {len(markets)}")
        filtered = []
        for m in markets:
            sym = m.get("symbol")
            if sym in config.CONFIG.trading.SYMBOLS:
                entry = {
                    "symbol": sym,
                    "price_precision": m.get("price_precision"),
                    "amount_precision": m.get("amount_precision"),
                    "flags": m.get("flags"),
                    "is_otc": m.get("is_otc"),
                    "is_market_type_enable": m.get("is_market_type_enable"),
                    "is_zero_fee": m.get("is_zero_fee"),
                    "quote_volume_24h": m.get("quote_volume_24h"),
                    "otc_min_notional": m.get("otc_min_notional"),
                    "otc_max_notional": m.get("otc_max_notional"),
                }
                print(f"  {sym}: {entry}")
                filtered.append(entry)
        save_json("probe_markets_symbols.json", filtered)
    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(0.3)

    # ---- b) Symbol format discovery ----
    print("\n--- b) Symbol format discovery ---")
    first_sym = config.CONFIG.trading.SYMBOLS[0] if config.CONFIG.trading.SYMBOLS else "BTCTMN"
    variants = [
        first_sym,
        first_sym.replace("TMN", "/TMN"),
        first_sym.replace("TMN", "-TMN"),
        first_sym.replace("TMN", "_TMN"),
    ]
    working_depth_sym = None
    working_hist_sym = None

    for sym in variants:
        print(f"  trying depth with '{sym}'")
        try:
            depth = call_with_latency(client.get_depth, sym)
            print(f"    depth OK: {len(depth['bids'])} bids, {len(depth['asks'])} asks")
            working_depth_sym = sym
            # save top 5 levels
            save_json("probe_depth_first.json", {
                "symbol": sym,
                "bids": depth["bids"][:5],
                "asks": depth["asks"][:5],
            })
            break
        except WallexError as e:
            print(f"    depth failed: {e}")
        except Exception as e:
            print(f"    depth error: {e}")
        time.sleep(0.3)

    for sym in variants:
        print(f"  trying history with '{sym}'")
        try:
            # last 6 hours
            to_ts = int(time.time())
            from_ts = to_ts - 6 * 3600
            candles = call_with_latency(client.get_candles, sym, "60", from_ts, to_ts)
            print(f"    history OK: {len(candles)} candles")
            working_hist_sym = sym
            break
        except WallexError as e:
            print(f"    history failed: {e}")
        except Exception as e:
            print(f"    history error: {e}")
        time.sleep(0.3)

    if not working_hist_sym:
        working_hist_sym = working_depth_sym or first_sym

    # ---- c) Resolutions ----
    print("\n--- c) Resolutions ---")
    resolutions = ["1", "5", "15", "60", "240", "D"]
    to_ts = int(time.time())
    from_ts = to_ts - 6 * 3600
    for res in resolutions:
        print(f"  resolution={res}")
        try:
            candles = call_with_latency(client.get_candles, working_hist_sym, res, from_ts, to_ts)
            print(f"    count={len(candles)}")
            if candles:
                first = candles[0]
                last = candles[-1]
                print(f"    first t={first['t']} o={first['o']}")
                print(f"    last  t={last['t']} c={last['c']}")
            if res == "15":
                save_json("probe_candles_first_15.json", {
                    "symbol": working_hist_sym,
                    "resolution": res,
                    "candles": candles[-5:],
                })
        except Exception as e:
            print(f"    error: {e}")
        time.sleep(0.3)

    # ---- d) Depth analysis per symbol ----
    print("\n--- d) Depth analysis ---")
    for sym in config.CONFIG.trading.SYMBOLS:
        print(f"  {sym}")
        try:
            depth = call_with_latency(client.get_depth, sym)
            bids = depth["bids"]
            asks = depth["asks"]
            if not bids or not asks:
                print("    empty book")
                continue
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            mid = (best_bid + best_ask) / 2
            spread_pct = (best_ask - best_bid) / mid * 100
            # depth within 0.5% and 1% of mid
            def depth_within(pct):
                limit_price_low = mid * (1 - pct / 100)
                limit_price_high = mid * (1 + pct / 100)
                bid_val = sum(p * q for p, q, _ in bids if p >= limit_price_low)
                ask_val = sum(p * q for p, q, _ in asks if p <= limit_price_high)
                return bid_val, ask_val
            d05_bid, d05_ask = depth_within(0.5)
            d1_bid, d1_ask = depth_within(1.0)
            print(f"    best_bid={best_bid:.4f} best_ask={best_ask:.4f} spread={spread_pct:.4f}%")
            print(f"    0.5% depth: bid TMN={d05_bid:.2f} ask TMN={d05_ask:.2f}")
            print(f"    1% depth:   bid TMN={d1_bid:.2f} ask TMN={d1_ask:.2f}")
            print(f"    top3 bids: {bids[:3]}")
            print(f"    top3 asks: {asks[:3]}")
        except Exception as e:
            print(f"    error: {e}")
        time.sleep(0.3)

    # ---- e) get_fee ----
    print("\n--- e) get_fee ---")
    try:
        fee = call_with_latency(client.get_fee)
        print(f"  maker_rate={fee.get('maker_rate')} taker_rate={fee.get('taker_rate')}")
        print(f"  raw keys: {list(fee.get('raw', {}).keys())[:5]}")
    except Exception as e:
        print(f"  error: {e}")
    time.sleep(0.3)

    # ---- f) get_balances ----
    print("\n--- f) get_balances ---")
    try:
        balances = call_with_latency(client.get_balances)
        non_zero = {a: v for a, v in balances.items() if v["free"] > 0 or v["locked"] > 0}
        for a, v in non_zero.items():
            print(f"  {a}: free={v['free']} locked={v['locked']}")
        # save shape with zero amounts
        shape = {a: {"free": 0.0, "locked": 0.0} for a in balances.keys()}
        save_json("probe_balances_shape.json", shape)
    except Exception as e:
        print(f"  error: {e}")
    time.sleep(0.3)

    # ---- g) Rate limit headers & latency stats ----
    print("\n--- g) Rate-limit headers & latency ---")
    rl_headers = {k: v for k, v in client.last_headers.items() if "ratelimit" in k.lower() or "retry-after" in k.lower()}
    if rl_headers:
        for k, v in rl_headers.items():
            print(f"  {k}: {v}")
    else:
        print("  (no rate-limit headers observed)")
    if latencies:
        print(f"  latency ms: min={min(latencies):.1f} avg={sum(latencies)/len(latencies):.1f} max={max(latencies):.1f}")

    print("\n=== SUMMARY ===")
    print(f"Symbols probed: {config.CONFIG.trading.SYMBOLS}")
    print(f"Working depth symbol format: {working_depth_sym}")
    print(f"Working history symbol format: {working_hist_sym}")
    print(f"Total API calls: {len(latencies)}")
    print("Done.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wallex read-only probe")
    args = parser.parse_args()
    main()