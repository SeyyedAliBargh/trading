"""Model input builder and system prompt (ai/prompt.py, PROMPT_VERSION v2).

build_input() assembles the exact dict that is serialized (compact JSON) as
the user message for every model. Every section sits behind a flag so the
input can be trimmed without touching callers; the news section stays OFF
(config.trading.USE_NEWS is False and there is no news fetcher yet).

v2 changes: the system prompt is regime-aware and tells the models NOT to
pre-empt the risk guard on costs (the guard owns fees/spread/minimum edge);
the input gains a "limits" section carrying the SAME config values the guard
clamps stop/target with, so the models can size inside them; recent
decisions are OFF by default (avoid anchoring on HOLD streaks).

Compactness rules: numbers are rounded (6 decimals for features/costs, 2
for prices, 4 for volumes), candles become [t, o, h, l, c, v] arrays, and
the JSON is serialized with separators=(",", ":") -- no pretty-print. Target:
2-4k tokens (estimate_tokens() is the shared estimator).
"""
import json
from typing import Any, Dict, List, Optional

import config

PROMPT_VERSION = "v2"

SYSTEM_PROMPT = (
    "You are a SPOT trading agent on the Wallex crypto exchange. Long-only: "
    "no shorts, no leverage. "
    "Use ONLY the numbers provided in the input; never invent or assume data. "
    "Actions: BUY (only when no position is open), SELL (only to close an open "
    "position), HOLD. "
    "A separate risk guard enforces fees, spread and minimum edge. Do NOT "
    "reject a setup only because costs exist. Your job is to estimate the "
    "probability that the target is reached before the stop within a holding "
    "horizon of a few hours. confidence is that probability; be calibrated, "
    "do not inflate it. "
    "stop_pct and target_pct are fractions of the entry price and MUST lie "
    "inside limits.stop_pct_range and limits.target_pct_range from the input. "
    "The guard clamps values into these ranges, so judge your confidence for "
    "the clamped values. Size them to current volatility: stop about 1 to 1.5 "
    "times the typical move (use features.atr_pct_15m), target about twice "
    "the stop, then clamp into the ranges. "
    "Use features.vol_regime_15m. High volatility with trend and rising "
    "volume: be willing to BUY at confidence >= 0.55. Normal: require trend "
    "alignment on the 15m and 1h series. Low volatility, or a target that "
    "needs a move many times larger than the recent ATR: the target is "
    "unlikely, so HOLD. "
    "Return BUY when trend, momentum, volume and book evidence support "
    "confidence >= 0.55 (for example features.ema20_15m above "
    "features.ema50_15m with features.rsi14_15m between 50 and 70, "
    "features.rel_volume_15m above normal, a breakout or a "
    "pullback-and-bounce, and a supportive features.book_imbalance_top10). "
    "Return HOLD only when there is genuinely no edge (downtrend, overheated "
    "RSI, wide spread, no volume, target out of reach). Over-passivity is "
    "also a failure: trade when edge exists. "
    'Respond with ONLY JSON: {"action": "BUY"|"SELL"|"HOLD", '
    '"confidence": number in [0,1], "stop_pct": number, '
    '"target_pct": number, "reason": short string, max 200 characters}. '
    "No markdown, no extra text."
)

DEFAULT_FLAGS: Dict[str, bool] = {
    "use_features": True,
    "use_book": True,
    "use_costs": True,
    "use_candles_1h": True,
    "use_candles_4h": True,
    # OFF since v2: recent decisions anchor the models on HOLD streaks.
    # The code path stays: pass flags={"use_recent_decisions": True} to include it.
    "use_recent_decisions": False,
    # USE_NEWS stays off: no news fetcher exists yet (config.trading.USE_NEWS).
    "use_news": False,
}


def _rf(value: Any, digits: int = 6) -> Any:
    """Round floats; pass everything else through (None stays None)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return value


def compact_candles(candles: Optional[List[Dict[str, Any]]], limit: int) -> List[List[Any]]:
    """Candles -> [[t, o, h, l, c, v], ...] (last `limit`, compact numbers)."""
    if not candles:
        return []
    out: List[List[Any]] = []
    for c in candles[-limit:]:
        try:
            out.append([
                int(c["t"]), _rf(c["o"], 2), _rf(c["h"], 2),
                _rf(c["l"], 2), _rf(c["c"], 2), _rf(c["v"], 4),
            ])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _compact_dict(d: Optional[Dict[str, Any]], digits: int = 6) -> Optional[Dict[str, Any]]:
    if not d:
        return None
    return {k: _rf(v, digits) for k, v in d.items()}


def compact_decisions(rows: Optional[List[Dict[str, Any]]], limit: int = 3) -> List[Dict[str, Any]]:
    """Store decision rows -> small {"ts", "action", "conf", "outcome"} dicts."""
    if not rows:
        return []
    out: List[Dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        out.append({
            "ts": str(row.get("ts", "")),
            "action": row.get("final_action"),
            "conf": _rf(row.get("confidence")),
            "outcome": row.get("outcome"),
        })
    return out


def limits_from_config(cfg: Any) -> Dict[str, Any]:
    """The 'limits' section, built from the SAME risk-config values the guard
    clamps stop/target with (single source of truth: config.risk)."""
    risk = cfg.risk
    return {
        "stop_pct_range": [float(risk.stop_pct_min), float(risk.stop_pct_max)],
        "target_pct_range": [float(risk.target_pct_min), float(risk.target_pct_max)],
        "min_net_edge_pct": float(risk.min_net_edge_pct),
    }


def build_input(symbol: str,
                features: Dict[str, Any],
                candles_15m: List[Dict[str, Any]],
                candles_1h: List[Dict[str, Any]],
                candles_4h: List[Dict[str, Any]],
                book: Dict[str, Any],
                costs: Dict[str, Any],
                position: Optional[Dict[str, Any]],
                recent_decisions: Optional[List[Dict[str, Any]]],
                limits: Optional[Dict[str, Any]] = None,
                flags: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """Assemble the model input dict (stored verbatim in decisions.input_json).

    `recent_decisions` are store rows (e.g. store.db.get_recent_decisions);
    `position` is None when flat, otherwise the open-position summary.
    `limits` is the guard's clamp config (see limits_from_config); when None
    it is derived from config.CONFIG at call time.
    `flags` override DEFAULT_FLAGS section by section.
    """
    fl = dict(DEFAULT_FLAGS)
    if flags:
        fl.update(flags)
    if limits is None:
        limits = limits_from_config(config.CONFIG)

    if position:
        rules = (f"Open position on {symbol}. Allowed actions: SELL (to close "
                 "the position) or HOLD. Do not BUY.")
    else:
        # flat: guidance deliberately contains no "SELL"
        rules = f"No open position on {symbol}. Allowed actions: BUY or HOLD."

    out: Dict[str, Any] = {
        "prompt_version": PROMPT_VERSION,
        "symbol": symbol,
        "rules": rules,
        "limits": _compact_dict(limits),
    }

    candles: Dict[str, Any] = {"15m": compact_candles(candles_15m, 12)}
    if fl["use_candles_1h"]:
        candles["1h"] = compact_candles(candles_1h, 10)
    if fl["use_candles_4h"]:
        candles["4h"] = compact_candles(candles_4h, 8)
    out["candles"] = candles

    if fl["use_features"]:
        out["features"] = {k: _rf(v) for k, v in (features or {}).items()}
    if fl["use_book"]:
        out["book"] = _compact_dict(book)
    if fl["use_costs"]:
        out["costs"] = _compact_dict(costs)
    out["position"] = _compact_dict(position)
    if fl["use_recent_decisions"]:
        out["recent_decisions"] = compact_decisions(recent_decisions, limit=3)
    if fl["use_news"]:
        # No news fetcher exists yet; the section stays a placeholder.
        out["news"] = None
    return out


def serialize_input(input_json: Dict[str, Any]) -> str:
    """Compact JSON for the wire: no spaces, no pretty-print."""
    return json.dumps(input_json, separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(input_json: Dict[str, Any]) -> int:
    """Rough token estimate: compact-serialized characters / 4."""
    return max(1, len(serialize_input(input_json)) // 4)
