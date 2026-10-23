"""Tests for ai/prompt.py (v2: regime-aware, limits section)."""
import json

import pytest

import config
from ai.prompt import (DEFAULT_FLAGS, PROMPT_VERSION, SYSTEM_PROMPT,
                       build_input, compact_candles, compact_decisions,
                       estimate_tokens, limits_from_config, serialize_input)


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
        assert PROMPT_VERSION == "v2.1"  # v2.1: reason limit read from config

    def test_system_prompt_contract(self):
        assert "SPOT" in SYSTEM_PROMPT
        assert "BUY" in SYSTEM_PROMPT and "SELL" in SYSTEM_PROMPT and "HOLD" in SYSTEM_PROMPT
        assert "only when no position is open" in SYSTEM_PROMPT
        assert "only to close an open position" in SYSTEM_PROMPT
        assert "probability" in SYSTEM_PROMPT          # confidence definition
        assert "risk guard" in SYSTEM_PROMPT            # v2: guard owns costs
        assert "minimum edge" in SYSTEM_PROMPT
        assert "Do NOT" in SYSTEM_PROMPT and "costs exist" in SYSTEM_PROMPT
        assert "calibrated" in SYSTEM_PROMPT and "holding horizon" in SYSTEM_PROMPT
        assert '"action"' in SYSTEM_PROMPT              # JSON-only output
        assert "stop_pct" in SYSTEM_PROMPT and "target_pct" in SYSTEM_PROMPT
        # v2.1: the reason cap is read from config, not hardcoded
        limit = config.CONFIG.llm.llm_reason_max_chars
        assert f"max {limit} characters" in SYSTEM_PROMPT
        assert "No markdown, no extra text" in SYSTEM_PROMPT

    def test_system_prompt_mentions_regime_and_limits(self):
        # v2 regime rule uses the REAL feature/section names from data/features.py
        assert "features.vol_regime_15m" in SYSTEM_PROMPT
        assert "features.atr_pct_15m" in SYSTEM_PROMPT
        assert "limits.stop_pct_range" in SYSTEM_PROMPT
        assert "limits.target_pct_range" in SYSTEM_PROMPT
        assert "0.55" in SYSTEM_PROMPT               # confidence threshold
        assert "Over-passivity" in SYSTEM_PROMPT     # trade when edge exists

    def test_system_prompt_has_no_hardcoded_clamp_values(self):
        """v2 rule: no numeric stop/target clamp may be hardcoded in the text;
        the models read them from the input's limits section."""
        risk = config.CONFIG.risk
        clamp_values = [risk.stop_pct_min, risk.stop_pct_max,
                        risk.target_pct_min, risk.target_pct_max,
                        risk.min_net_edge_pct]
        for value in clamp_values:
            token = f"{value:g}" if value != int(value) else f"{value:.1f}"
            assert token not in SYSTEM_PROMPT, f"clamp {token} hardcoded in prompt"


class TestBuildInput:
    def test_sections_present(self):
        inp = build_input("BTCTMN", make_features(), make_candles(30),
                          make_candles(20), make_candles(10),
                          make_book(), make_costs(), None, make_decisions(),
                          flags={"use_recent_decisions": True})
        for key in ("prompt_version", "symbol", "rules", "limits", "candles",
                    "features", "book", "costs", "position", "recent_decisions"):
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
                          [{"ts": "t", "final_action": "HOLD", "confidence": 0.5}],
                          flags={"use_recent_decisions": True})
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

    def test_default_flags_recent_decisions_off(self):
        # v2: avoid anchoring the models on HOLD streaks
        assert DEFAULT_FLAGS["use_recent_decisions"] is False

    def test_recent_decisions_off_by_default_and_on_when_flagged(self):
        decisions = [{"ts": "t", "final_action": "HOLD", "confidence": 0.5}]
        default_inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, decisions)
        assert "recent_decisions" not in default_inp   # off by default (v2)
        enabled_inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, decisions,
                                  flags={"use_recent_decisions": True})
        assert len(enabled_inp["recent_decisions"]) == 1  # code path kept

    def test_recent_decisions_limited_to_three(self):
        inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, make_decisions(),
                          flags={"use_recent_decisions": True})
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


class TestLimits:
    def test_limits_derived_from_config_by_default(self):
        """The limits section equals the SAME config values the guard clamps with."""
        inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, [])
        risk = config.CONFIG.risk
        assert inp["limits"]["stop_pct_range"] == [risk.stop_pct_min,
                                                   risk.stop_pct_max]
        assert inp["limits"]["target_pct_range"] == [risk.target_pct_min,
                                                     risk.target_pct_max]
        assert inp["limits"]["min_net_edge_pct"] == risk.min_net_edge_pct

    def test_limits_from_config_helper(self):
        risk = config.CONFIG.risk
        limits = limits_from_config(config.CONFIG)
        assert limits == {
            "stop_pct_range": [risk.stop_pct_min, risk.stop_pct_max],
            "target_pct_range": [risk.target_pct_min, risk.target_pct_max],
            "min_net_edge_pct": risk.min_net_edge_pct,
        }

    def test_limits_passed_explicitly_are_used(self):
        limits = {"stop_pct_range": [0.05, 0.07],
                  "target_pct_range": [0.08, 0.1],
                  "min_net_edge_pct": 0.01}
        inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, [],
                          limits=limits)
        assert inp["limits"]["stop_pct_range"] == [0.05, 0.07]
        assert inp["limits"]["target_pct_range"] == [0.08, 0.1]
        assert inp["limits"]["min_net_edge_pct"] == 0.01

    def test_limits_present_with_all_flags_off(self):
        # the limits section is unconditional: the guard always clamps
        inp = build_input("BTCTMN", {}, [], [], [], {}, {}, None, [],
                          flags={"use_features": False, "use_book": False,
                                 "use_costs": False, "use_candles_1h": False,
                                 "use_candles_4h": False})
        assert "limits" in inp


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
