"""Tests for ai/prompt.py (STEP 4d PART 4)."""
import json

import pytest

from ai.prompt import (DEFAULT_FLAGS, PROMPT_VERSION, SYSTEM_PROMPT,
                       build_input, compact_candles, compact_decisions,
                       estimate_tokens, serialize_input)


def make_candles(n, base_t=1_700_000_000, step=900, price=100_000.0):
    return [{"t": base_t + i * step, "o": price + i, "h": price + i + 10,
             "l": price + i - 10, "c": price + i + 5, "v": 123.4567 + i}
            for i in range(n)]


def make_features():
    return {"close_15m": 100_005.0, "ema20_15m": 99_000.123456,
            "ema50_15m": 98_000.0, "rsi14_15m": 55.5, "atr14_15m": 300.0,
            "atr_pct_15m": 0.003, "rel_volume_15m": 1.2,
            "vol_regime_15m": "normal", "spread_pct": 0.002,
            "book_imbalance_top10": 0.1}


def make_costs():
    return {"taker_fee": 0.003, "spread_pct": 0.002, "round_trip_cost": 0.008}


def make_book():
    return {"best_bid": 100_000.0, "best_ask": 100_200.0, "spread_pct": 0.002,
            "bid_depth_1pct": 1_500_000.0, "ask_depth_1pct": 1_400_000.0}


def make_decisions(n=3):
    return [{"ts": "2026-10-23T10:00:00+00:00", "final_action": "HOLD",
             "confidence": 0.55, "outcome": None, "input_json": "x" * 5000},
            {"ts": "2026-10-23T09:45:00+00:00", "final_action": "BUY",
             "confidence": 0.72, "outcome": "entered", "input_json": "y" * 5000},
            {"ts": "2026-10-23T09:30:00+00:00", "final_action": "HOLD",
             "confidence": 0.61, "outcome": None, "input_json": "z" * 5000},
            {"ts": "2026-10-23T09:15:00+00:00", "final_action": "HOLD",
             "confidence": 0.5, "outcome": None, "input_json": "w" * 5000}][:n + 1][:n] if n < 4 else []


class TestConstants:
    def test_prompt_version(self):
        assert PROMPT_VERSION == "v1"

    def test_system_prompt_contract(self):
        assert "SPOT" in SYSTEM_PROMPT
        assert "BUY" in SYSTEM_PROMPT and "SELL" in SYSTEM_PROMPT and "HOLD" in SYSTEM_PROMPT
        assert "only when no position is open" in SYSTEM_PROMPT
        assert "only to close an open position" in SYSTEM_PROMPT
        assert "probability" in SYSTEM_PROMPT          # confidence definition
        assert "round-trip cost" in SYSTEM_PROMPT       # must beat costs
        assert '"action"' in SYSTEM_PROMPT              # JSON-only output
        assert "stop_pct" in SYSTEM_PROMPT and "target_pct" in SYSTEM_PROMPT
        assert "200" in SYSTEM_PROMPT                    # reason length cap


class TestBuildInput:
    def test_sections_present(self):
        inp = build_input("BTCTMN", make_features(), make_candles(30),
                          make_candles(20), make_candles(10),
                          make_book(), make_costs(), None, make_decisions())
        for key in ("prompt_version", "symbol", "rules", "candles", "features",
                    "book", "costs", "position", "recent_decisions"):
            assert key in inp
        assert "news" not in inp  # USE_NEWS stays off

    def test_candle_limits(self):
        inp = build_input("BTCTMN", make_features(), make_candles(30),
                          make_candles(20), make_candles(10),
                          make_book(), make_costs(), None, [])
        assert len(inp["candles"]["15m"]) == 12   # last 12
        assert len(inp["candles"]["1h"]) == 10    # last 10
        assert len(inp["candles"]["4h"]) == 8     # last 8

    def test_candles_compact_arrays(self):
        inp = build_input("BTCTMN", {}, make_candles(2), [], [], {}, {}, None, [])
        c = inp["candles"]["15m"][0]
        assert isinstance(c, list) and len(c) == 6  # [t,o,h,l,c,v]
        assert isinstance(c[0], int)

    def test_flat_state_has_no_sell_guidance(self):
        inp = build_input("BTCTMN", make_features(), make_candles(12),
                          make_candles(10), make_candles(8),
                          make_book(), make_costs(), None,
                          [{"ts": "t", "final_action": "HOLD", "confidence": 0.5}])
        dump = json.dumps(inp)
        assert "SELL" not in dump
        assert "BUY or HOLD" in inp["rules"]

    def test_open_position_guidance_mentions_sell(self):
        position = {"entry_price": 100_000.0, "quantity": 0.01,
                    "stop_pct": 0.015, "target_pct": 0.045,
                    "unrealized_pct": 0.004}
        inp = build_input("BTCTMN", make_features(), make_candles(12),
                          make_candles(10), make_candles(8),
                          make_book(), make_costs(), position, [])
        assert "SELL" in inp["rules"]
        assert "Do not BUY" in inp["rules"]
        assert inp["position"]["entry_price"] == 100_000.0

    def test_flags_drop_sections(self):
        inp = build_input("BTCTMN", make_features(), make_candles(12),
                          make_candles(10), make_candles(8),
                          make_book(), make_costs(), None, make_decisions(),
                          flags={"use_candles_1h": False, "use_candles_4h": False,
                                 "use_features": False, "use_book": False,
                                 "use_costs": False, "use_recent_decisions": False})
        assert set(inp["candles"].keys()) == {"15m"}
        for key in ("features", "book", "costs", "recent_decisions"):
            assert key not in inp

    def test_flag_use_news_adds_placeholder(self):
        inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, [],
                          flags={"use_news": True})
        assert "news" in inp and inp["news"] is None

    def test_default_flags_news_off(self):
        assert DEFAULT_FLAGS["use_news"] is False

    def test_recent_decisions_limited_to_three(self):
        inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, make_decisions())
        assert len(inp["recent_decisions"]) == 3
        assert set(inp["recent_decisions"][0].keys()) == {"ts", "action", "conf", "outcome"}
        # the huge input_json blob must NOT leak into the prompt
        assert "x" * 100 not in json.dumps(inp)

    def test_numbers_rounded(self):
        features = {"ema20_15m": 99_123.123456789, "rsi14_15m": 55.123456789,
                    "vol_regime_15m": "normal"}
        inp = build_input("BTCTMN", features, [], [], [], {"best_bid": 100.123456789},
                          {"taker_fee": 0.003333333}, None, [])
        assert inp["features"]["ema20_15m"] == 99123.123457
        assert inp["features"]["rsi14_15m"] == 55.123457
        assert inp["features"]["vol_regime_15m"] == "normal"
        assert inp["book"]["best_bid"] == 100.123457
        assert inp["costs"]["taker_fee"] == 0.003333


class TestTokens:
    def test_token_estimate_within_target(self):
        inp = build_input("BTCTMN", make_features(), make_candles(30),
                          make_candles(20), make_candles(10),
                          make_book(), make_costs(), None, make_decisions())
        est = estimate_tokens(inp)
        assert 100 <= est <= 4000  # target 2-4k ceiling, non-trivial floor

    def test_flags_reduce_estimate(self):
        full = build_input("BTCTMN", make_features(), make_candles(30),
                           make_candles(20), make_candles(10),
                           make_book(), make_costs(), None, make_decisions())
        trimmed = build_input("BTCTMN", make_features(), make_candles(30),
                              make_candles(20), make_candles(10),
                              make_book(), make_costs(), None, make_decisions(),
                              flags={"use_candles_1h": False, "use_candles_4h": False,
                                     "use_recent_decisions": False})
        assert estimate_tokens(trimmed) < estimate_tokens(full)

    def test_serialize_compact(self):
        inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, [])
        compact = serialize_input(inp)
        pretty = json.dumps(inp, indent=2)
        assert len(compact) < len(pretty)
        assert '": ' not in compact and '", "' not in compact


class TestCompactHelpers:
    def test_compact_candles_limit_and_garbage(self):
        out = compact_candles(make_candles(5), 3)
        assert len(out) == 3
        assert compact_candles([{"bad": 1}, {"t": 1}], 2) == [[1]] or \
            compact_candles([{"bad": 1}], 2) == []

    def test_compact_decisions_none_and_non_dict(self):
        assert compact_decisions(None) == []
        assert compact_decisions(["junk", {"ts": "t", "final_action": "BUY"}]) == [
            {"ts": "t", "action": "BUY", "conf": None, "outcome": None}]
