#!/usr/bin/env python3
"""LLM probe (STEP 4d PART 7). Ali runs this -- it makes REAL network calls.

Sends a tiny fixed prompt to every configured model, twice per model: once
without JSON mode and once with response_format {"type": "json_object"}.
Prints latency and whether strictly valid vote JSON came back.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse

import config
from ai.models import BoxApiClient
from ai.prompt import SYSTEM_PROMPT

FIXED_PROMPT_TEMPLATE = (
    "Market: {symbol} (Wallex spot, long-only; quote TMN). "
    "Snapshot: 15m close=100000, rsi14=55, atr_pct=0.003, spread_pct=0.002, "
    "taker_fee=0.003, round_trip_cost=0.008. No open position. "
    "Allowed actions: BUY or HOLD. "
    'Respond with ONLY JSON: {{"action": "BUY"|"HOLD", "confidence": 0..1, '
    '"stop_pct": number, "target_pct": number, "reason": "..."}}'
)


def build_fixed_prompt(symbol: str) -> str:
    return FIXED_PROMPT_TEMPLATE.format(symbol=symbol)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="LLM probe: tiny fixed prompt to each model, with/without JSON mode")
    parser.add_argument("--timeout", type=float, default=None,
                        help="Per-model timeout override (seconds)")
    parser.add_argument("--models", nargs="*", default=None,
                        help="Models to probe (default: CONFIG.llm.MODELS)")
    args = parser.parse_args()

    if not config.CONFIG.llm.api_key:
        print("ERROR: BOXAPI_API_KEY is not set; cannot probe. (The key is never printed.)")
        sys.exit(1)

    symbol = (config.CONFIG.trading.SYMBOLS or ["<no-symbol>"])[0]
    prompt = build_fixed_prompt(symbol)
    models = args.models or list(config.CONFIG.llm.MODELS)

    print("=== LLM probe ===")
    print(f"Base URL: {config.CONFIG.llm.LLM_BASE_URL}")
    print(f"Models: {models}")
    print(f"Config LLM_JSON_MODE: {config.CONFIG.trading.LLM_JSON_MODE}")
    print(f"Fixed prompt: {prompt}\n")

    client = BoxApiClient(config.CONFIG)
    summary = []
    for model in models:
        for json_mode in (False, True):
            res = client.ask(model, SYSTEM_PROMPT, prompt,
                             json_mode=json_mode, timeout=args.timeout)
            valid_json = bool(res["ok"] and res.get("parsed"))
            mode = "json_object" if json_mode else "plain"
            print(f"[{model}] mode={mode}: valid_json={valid_json} "
                  f"latency={res['latency_ms']:.0f}ms attempts={res['attempts']} "
                  f"error={res['error']}")
            if res.get("content"):
                print(f"  raw (first 200 chars): {res['content'][:200]}")
            if res.get("parsed"):
                print(f"  parsed: {res['parsed']}")
            summary.append((model, mode, valid_json, res["latency_ms"]))

    print("\n=== SUMMARY ===")
    for model, mode, valid_json, latency in summary:
        print(f"{model:20s} mode={mode:11s} valid_json={valid_json} latency={latency:.0f}ms")
    n_valid = sum(1 for _, _, v, _ in summary if v)
    print(f"Valid JSON responses: {n_valid}/{len(summary)}")
    print("Probe finished.")


if __name__ == "__main__":
    main()
