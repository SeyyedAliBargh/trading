#!/usr/bin/env python3
"""Wallex read-only probe (updated STEP 4b)."""
import sys
from pathlib import Path

# Make project imports work regardless of cwd / invocation style
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import statistics
import time
from typing import Dict, List, Any

import config
from exchange.wallex import WallexClient, WallexError

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "docs" / "samples"
SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

RESOLUTIONS = ["1", "3", "5", "15", "30", "60", "120", "180", "240", "360", "720", "1D", "D", "W"]
WINDOWS_H = [6, 72]


def save_json(name: str, data) -> None:
    path = SAMPLE_DIR / name
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  saved -> {path}")


def symbol_variants(symbol: str) -> List[str]:
    return [
        symbol,
        symbol.replace("TMN", "/TMN"),
        symbol.replace("TMN", "-TMN"),
        symbol.replace("TMN", "_TMN"),
    ]


def median_delta(candles: List[Dict]) -> float:
    if len(candles) < 2:
        return 0.0
    deltas = [b["t"] - a["t"] for a, b in zip(candles, candles[1:])]
    return statistics.median(deltas)


def main() -> None:
    print("=== Wallex Read-Only Probe (STEP 4b) ===")
    print(f"Base URL: {config.CONFIG.exchange.WALLEX_BASE_URL}")
    print(f"Symbols: {config.CONFIG.trading.SYMBOLS}")
    client = WallexClient(config.CONFIG, db=None)

    latencies: List[float] = []
    findings: Dict[str, Any] = {}

    def timed(fn, *a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            latencies.append((time.perf_counter() - t0) * 1000)

    # ------------------------------------------------------------------ a)
    print("\n--- a) markets ---")
    try:
        markets = timed(client.get_markets)
        print(f"Total markets: {len(markets)}")
        entries = []
        for m in markets:
            if isinstance(m, dict) and m.get("symbol") in config.CONFIG.trading.SYMBOLS:
                entries.append({k: m.get(k) for k in (
                    "symbol", "price_precision", "amount_precision", "flags", "is_otc",
                    "is_market_type_enable", "is_zero_fee", "quote_volume_24h",
                    "otc_min_notional", "otc_max_notional")})
                print(f"  {m['symbol']}: pp={m.get('price_precision')} ap={m.get('amount_precision')} "
                      f"mte={m.get('is_market_type_enable')} zf={m.get('is_zero_fee')} "
                      f"qv24={m.get('quote_volume_24h')}")
        save_json("probe_markets_symbols.json", entries)
    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(0.3)

    # ------------------------------------------------------------------ b)
    print("\n--- b) symbol format ---")
    working = {}
    for sym in config.CONFIG.trading.SYMBOLS:
        for variant in symbol_variants(sym):
            try:
                depth = timed(client.get_depth, variant)
                print(f"  depth OK with '{variant}' ({len(depth['bids'])} bids)")
                working[sym] = variant
                if sym == config.CONFIG.trading.SYMBOLS[0]:
                    save_json("probe_depth_first.json", {
                        "symbol": variant,
                        "bids": depth["bids"][:5],
                        "asks": depth["asks"][:5],
                    })
                break
            except Exception as e:
                print(f"  depth FAILED with '{variant}': {e}")
            time.sleep(0.3)
    findings["symbol_format"] = working

    # ------------------------------------------------------------------ c)
    print("\n--- c) resolutions ---")
    first_symbol = config.CONFIG.trading.SYMBOLS[0]
    candle_results: Dict[str, Any] = {}
    previous_payload = None
    for window_h in WINDOWS_H:
        to_ts = int(time.time())
        from_ts = to_ts - window_h * 3600
        for res in RESOLUTIONS:
            try:
                candles = timed(client.get_candles, first_symbol, res, from_ts, to_ts)
                key = f"{window_h}h_{res}"
                delta = median_delta(candles)
                identical = (previous_payload == candles) if previous_payload is not None else False
                print(f"  w={window_h}h res={res}: count={len(candles)} "
                      f"first_t={candles[0]['t'] if candles else None} "
                      f"last_t={candles[-1]['t'] if candles else None} "
                      f"median_delta={delta}s identical_to_prev={identical}")
                candle_results[key] = {"count": len(candles),
                                       "first_t": candles[0]["t"] if candles else None,
                                       "last_t": candles[-1]["t"] if candles else None,
                                       "median_delta_sec": delta,
                                       "identical_to_previous": identical,
                                       "candles": candles[-5:]}
                previous_payload = candles
            except Exception as e:
                print(f"  w={window_h}h res={res}: ERROR {e}")
                candle_results[f"{window_h}h_{res}"] = {"error": str(e)}
            time.sleep(0.3)
    # per-symbol res 1 and 60 for 72h
    for sym in config.CONFIG.trading.SYMBOLS:
        for res in ("1", "60"):
            to_ts = int(time.time())
            from_ts = to_ts - 72 * 3600
            try:
                candles = timed(client.get_candles, sym, res, from_ts, to_ts)
                key = f"72h_{sym}_{res}"
                print(f"  {sym} res={res}: count={len(candles)}")
                candle_results[key] = {"count": len(candles),
                                       "first_t": candles[0]["t"] if candles else None,
                                       "last_t": candles[-1]["t"] if candles else None,
                                       "median_delta_sec": median_delta(candles),
                                       "candles": candles[-5:]}
            except Exception as e:
                print(f"  {sym} res={res}: ERROR {e}")
                candle_results[f"72h_{sym}_{res}"] = {"error": str(e)}
            time.sleep(0.3)
    save_json("probe_candles_resolutions.json", candle_results)

    # ------------------------------------------------------------------ d)
    print("\n--- d) depth analysis ---")
    for sym in config.CONFIG.trading.SYMBOLS:
        fmt = working.get(sym, sym)
        try:
            depth = timed(client.get_depth, fmt)
            bids, asks = depth["bids"], depth["asks"]
            if not bids or not asks:
                print(f"  {sym}: empty book")
                continue
            bb, ba = bids[0][0], asks[0][0]
            mid = (bb + ba) / 2
            spread = (ba - bb) / mid * 100
            def depth_within(levels, pct, side):
                if side == "bid":
                    lim = mid * (1 - pct / 100)
                    return sum(p * q for p, q, _ in levels if p >= lim)
                lim = mid * (1 + pct / 100)
                return sum(p * q for p, q, _ in levels if p <= lim)
            print(f"  {sym}: bb={bb} ba={ba} spread={spread:.4f}% "
                  f"bid0.5%={depth_within(bids,0.5,'bid'):.0f} ask0.5%={depth_within(asks,0.5,'ask'):.0f} "
                  f"bid1%={depth_within(bids,1,'bid'):.0f} ask1%={depth_within(asks,1,'ask'):.0f}")
        except Exception as e:
            print(f"  {sym}: ERROR {e}")
        time.sleep(0.3)

    # ------------------------------------------------------------------ e)
    print("\n--- e) fees (all symbols) ---")
    try:
        fees = timed(client.get_fee)
        for sym in config.CONFIG.trading.SYMBOLS:
            f = fees.get(sym)
            print(f"  {sym}: {f}")
        save_json("probe_fee.json", fees)
    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(0.3)

    # ------------------------------------------------------------------ f)
    print("\n--- f) market info ---")
    try:
        infos = {}
        for sym in config.CONFIG.trading.SYMBOLS:
            mi = client.get_market_info(sym)
            if mi is not None:
                d = {
                    "symbol": mi.symbol,
                    "base_asset": mi.base_asset,
                    "quote_asset": mi.quote_asset,
                    "price_precision": mi.price_precision,
                    "amount_precision": mi.amount_precision,
                    "is_market_type_enable": mi.is_market_type_enable,
                    "is_zero_fee": mi.is_zero_fee,
                    "flags": list(mi.flags),
                    "quote_volume_24h": mi.quote_volume_24h,
                }
                infos[sym] = d
                print(f"  {sym}: {d}")
            else:
                print(f"  {sym}: NOT FOUND in markets")
        save_json("probe_market_info.json", infos)
    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(0.3)

    # ------------------------------------------------------------------ g)
    print("\n--- g) balances ---")
    try:
        balances = timed(client.get_balances)
        non_zero = {a: v for a, v in balances.items() if v["free"] > 0 or v["locked"] > 0}
        for a, v in non_zero.items():
            print(f"  {a}: free={v['free']} locked={v['locked']}")
        shape = {a: {"free": 0.0, "locked": 0.0} for a in balances}
        save_json("probe_balances_shape.json", shape)
    except Exception as e:
        print(f"  ERROR: {e}")
    time.sleep(0.3)

    # ------------------------------------------------------------------ h)
    print("\n--- h) rate-limit headers & latency ---")
    rl = {k: v for k, v in client.last_headers.items()
          if "ratelimit" in k.lower() or "retry-after" in k.lower()}
    for k, v in rl.items():
        print(f"  {k}: {v}")
    if latencies:
        print(f"  latency ms: min={min(latencies):.0f} avg={sum(latencies)/len(latencies):.0f} "
              f"max={max(latencies):.0f} (n={len(latencies)})")

    print("\n=== SUMMARY ===")
    print(f"Symbol formats: {findings.get('symbol_format')}")
    if latencies:
        print(f"API calls: {len(latencies)}, avg latency {sum(latencies)/len(latencies):.0f} ms")
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wallex read-only probe (STEP 4b)")
    parser.parse_args()
    main()
