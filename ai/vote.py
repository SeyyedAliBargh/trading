"""Vote tallying (ai/vote.py).

tally() turns the per-model votes of one decision cycle into a single
VoteResult. BUY/SELL need at least `min_agreeing` identical actions, anything
else is HOLD. A SELL majority only counts when the models are allowed to
vote on exits (config MODELS_VOTE_ON_EXIT); the loop never even calls the
models while a position is open when that flag is off.

Invalid votes (ai.models degrades every failure to action="HOLD",
valid=False) are counted as HOLD and noted in the reasons.

Pure and deterministic: no db, no network, no clocks.
"""
import statistics
from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence


@dataclass(frozen=True)
class VoteResult:
    action: str                                   # BUY / SELL / HOLD
    confidence_mean: Optional[float]              # mean over agreeing models
    stop_pct_median: Optional[float]              # median over agreeing models
    target_pct_median: Optional[float]
    agreeing_models: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)


def _mean(values: Sequence[float]) -> Optional[float]:
    return float(sum(values) / len(values)) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    return float(statistics.median(values)) if values else None


def _nonNone(votes: Sequence[Any], attr: str) -> List[float]:
    out = []
    for v in votes:
        value = getattr(v, attr, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def tally(votes: Sequence[Any], min_agreeing: int,
          models_vote_on_exit: bool) -> VoteResult:
    """Aggregate model votes into one VoteResult (see module docstring)."""
    votes = list(votes or [])
    if not votes:
        return VoteResult("HOLD", None, None, None, [], ["no votes"])

    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for v in votes:
        action = getattr(v, "action", "HOLD")
        counts[action] = counts.get(action, 0) + 1
    reasons = [f"votes: {counts['BUY']} BUY / {counts['SELL']} SELL / {counts['HOLD']} HOLD"]

    invalid = [v for v in votes if getattr(v, "valid", True) is False]
    if invalid:
        reasons.append(f"{len(invalid)} invalid vote(s) counted as HOLD")

    candidates = [a for a in ("BUY", "SELL") if counts.get(a, 0) >= min_agreeing]
    if len(candidates) == 2 and counts["BUY"] == counts["SELL"]:
        return VoteResult("HOLD", None, None, None, [],
                          reasons + ["tie between BUY and SELL -> HOLD"])
    if candidates:
        action = "BUY" if (counts["BUY"] >= counts["SELL"] or "SELL" not in candidates) else "SELL"
        if action == "SELL" and not models_vote_on_exit:
            return VoteResult("HOLD", None, None, None, [],
                              reasons + ["SELL majority ignored: MODELS_VOTE_ON_EXIT is off"])
        agreeing = [v for v in votes if getattr(v, "action", None) == action]
        return VoteResult(
            action=action,
            confidence_mean=_mean(_nonNone(agreeing, "confidence")),
            stop_pct_median=_median(_nonNone(agreeing, "stop_pct")),
            target_pct_median=_median(_nonNone(agreeing, "target_pct")),
            agreeing_models=[getattr(v, "model", "?") for v in agreeing],
            reasons=reasons,
        )
    return VoteResult("HOLD", None, None, None, [],
                      reasons + [f"no action reached {min_agreeing} agreeing votes"])
