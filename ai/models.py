"""OpenAI-compatible LLM client (BoxAPI) for the deciding models (ai/models.py).

- Synchronous `requests` against {LLM_BASE_URL}/chat/completions with
  "Authorization: Bearer <key>"; the key is NEVER logged or stored.
- decide_all() fans out ONE request per model in a ThreadPoolExecutor with a
  per-model timeout, ONE retry on timeout/5xx/network errors and ONE re-ask
  when the 200-body is not strictly valid vote JSON.
- Any failure degrades to a HOLD vote with the error recorded (AGENTS.md hard
  rule 2: an error on the decision path is never an entry).
- Every call attempt is logged to the store's llm_calls table (model,
  latency, tokens, raw output truncated to 4k, parsed vote, error).

The HTTP layer is injectable (`transport`): a callable
(payload: dict, timeout: float) -> (status_code, parsed_json) that may raise
TimeoutError. Tests inject a fake transport; no test touches the network.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import config
from ai.prompt import SYSTEM_PROMPT, serialize_input

VALID_ACTIONS = frozenset({"BUY", "SELL", "HOLD"})
MAX_RAW_LOG_CHARS = 4096


class LlmError(Exception):
    """Invalid or unusable model output."""


@dataclass(frozen=True)
class ModelVote:
    """One model's vote. Invalid output becomes a HOLD vote with valid=False."""
    model: str
    action: str                      # always BUY/SELL/HOLD
    confidence: Optional[float]
    stop_pct: Optional[float]
    target_pct: Optional[float]
    reason: str
    valid: bool
    error: Optional[str] = None
    latency_ms: Optional[float] = None


def _hold_vote(model: str, error: str, latency_ms: Optional[float] = None) -> ModelVote:
    return ModelVote(model=model, action="HOLD", confidence=None, stop_pct=None,
                     target_pct=None, reason="invalid model output", valid=False,
                     error=error, latency_ms=latency_ms)


def parse_vote(content: str) -> Dict[str, Any]:
    """Strictly parse one model reply into a vote dict.

    Accepts a bare JSON object or JSON inside markdown fences / surrounding
    text, then validates: action in {BUY, SELL, HOLD}, confidence in [0, 1],
    stop_pct/target_pct numbers >= 0. Raises LlmError on anything else.
    """
    if not content or not isinstance(content, str):
        raise LlmError("empty content")
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end <= start:
        raise LlmError("no JSON object in content")
    try:
        data = json.loads(content[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LlmError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise LlmError("vote is not a JSON object")

    action = data.get("action")
    if not isinstance(action, str) or action.upper() not in VALID_ACTIONS:
        raise LlmError(f"bad action: {action!r}")
    confidence = data.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
            or not (0.0 <= float(confidence) <= 1.0):
        raise LlmError(f"confidence out of [0,1]: {confidence!r}")
    stop_pct = data.get("stop_pct")
    if not isinstance(stop_pct, (int, float)) or isinstance(stop_pct, bool) or float(stop_pct) < 0:
        raise LlmError(f"bad stop_pct: {stop_pct!r}")
    target_pct = data.get("target_pct")
    if not isinstance(target_pct, (int, float)) or isinstance(target_pct, bool) or float(target_pct) < 0:
        raise LlmError(f"bad target_pct: {target_pct!r}")
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        reason = str(reason)
    return {
        "action": action.upper(),
        "confidence": float(confidence),
        "stop_pct": float(stop_pct),
        "target_pct": float(target_pct),
        "reason": reason[:200],
    }


def _extract_content(resp: Any) -> Tuple[Optional[str], Optional[str]]:
    """choices[0].message.content from an OpenAI-style response."""
    try:
        content = resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        return None, f"no content in response: {exc}"
    if not isinstance(content, str) or not content.strip():
        return None, "empty content"
    return content, None


class BoxApiClient:
    """OpenAI-compatible chat-completions client with parallel model voting."""

    def __init__(self, cfg: Optional[config.Config] = None, db: Any = None,
                 transport: Optional[Callable[[Dict[str, Any], float],
                                              Tuple[int, Any]]] = None):
        self.cfg = cfg if cfg is not None else config.CONFIG
        self.db = db  # store module (store.db) or None; None disables logging
        self._transport = transport if transport is not None else self._default_transport

    # -- transport ---------------------------------------------------------
    def _default_transport(self, payload: Dict[str, Any],
                           timeout: float) -> Tuple[int, Any]:
        """Real HTTP POST. The Bearer key never appears in logs or the store."""
        url = self.cfg.llm.LLM_BASE_URL.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.cfg.llm.api_key or ''}",
            "Content-Type": "application/json",
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        try:
            body = resp.json()
        except ValueError:
            body = {"_raw_text": resp.text[:500]}
        return resp.status_code, body

    def _attempt(self, payload: Dict[str, Any], timeout: float) -> Tuple[Optional[int], Any, Optional[str]]:
        """One HTTP attempt -> (status, body, error). Never raises."""
        try:
            status, body = self._transport(payload, timeout)
            return int(status), body, None
        except (TimeoutError, requests.exceptions.Timeout):
            return None, None, "timeout"
        except Exception as exc:
            return None, None, f"network error: {exc}"

    # -- logging -------------------------------------------------------------
    def _log_call(self, model: str, decision_id: Optional[int], run_id: Optional[int],
                  raw: Optional[str], parsed: Optional[Dict[str, Any]], valid: bool,
                  tokens: Tuple[Optional[int], Optional[int]],
                  latency_ms: float, error: Optional[str]) -> None:
        if self.db is None:
            return
        try:
            self.db.insert_llm_call(
                run_id=run_id, decision_id=decision_id, model=model,
                raw_output=(raw or "")[:MAX_RAW_LOG_CHARS], parsed_json=parsed,
                valid=valid, prompt_tokens=tokens[0], completion_tokens=tokens[1],
                latency_ms=latency_ms, error=error,
            )
        except Exception:
            pass  # logging must never crash the bot (hard rule 9)

    @staticmethod
    def _usage_tokens(body: Any) -> Tuple[Optional[int], Optional[int]]:
        try:
            usage = body.get("usage") or {}
            return usage.get("prompt_tokens"), usage.get("completion_tokens")
        except AttributeError:
            return None, None

    # -- payload ---------------------------------------------------------------
    def _json_mode_enabled(self, json_mode: Optional[bool]) -> bool:
        if json_mode is not None:
            return bool(json_mode)
        return bool(self.cfg.trading.LLM_JSON_MODE)

    def _payload(self, model: str, input_json: Dict[str, Any],
                 json_mode: Optional[bool] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": serialize_input(input_json)},
            ],
        }
        if self._json_mode_enabled(json_mode):
            payload["response_format"] = {"type": "json_object"}
        return payload

    # -- single model decision -----------------------------------------------
    def _decide_one(self, model: str, input_json: Dict[str, Any],
                    decision_id: Optional[int], run_id: Optional[int]) -> ModelVote:
        payload = self._payload(model, input_json)
        timeout = float(self.cfg.llm.per_model_timeout_sec)
        total_latency = 0.0
        for attempt_no in (1, 2):
            t0 = time.perf_counter()
            status, body, err = self._attempt(payload, timeout)
            latency_ms = (time.perf_counter() - t0) * 1000
            total_latency += latency_ms
            tokens = self._usage_tokens(body) if body else (None, None)

            # transport-level failure (timeout / network)
            if err is not None:
                self._log_call(model, decision_id, run_id, raw=None, parsed=None,
                               valid=False, tokens=tokens, latency_ms=latency_ms, error=err)
                if attempt_no == 1:
                    continue  # ONE retry on timeout/network
                return _hold_vote(model, err, total_latency)

            # HTTP-level failure
            if status != 200:
                err = f"http_{status}"
                self._log_call(model, decision_id, run_id, raw=json.dumps(body)[:MAX_RAW_LOG_CHARS],
                               parsed=None, valid=False, tokens=tokens,
                               latency_ms=latency_ms, error=err)
                if status >= 500 and attempt_no == 1:
                    continue  # ONE retry on 5xx
                return _hold_vote(model, err, total_latency)

            # 200: parse the vote strictly
            content, extract_err = _extract_content(body)
            if extract_err is None:
                try:
                    parsed = parse_vote(content)
                except LlmError as exc:
                    parsed, extract_err = None, str(exc)
            else:
                parsed = None
            if extract_err is not None:
                self._log_call(model, decision_id, run_id, raw=content, parsed=parsed,
                               valid=False, tokens=tokens, latency_ms=latency_ms,
                               error=extract_err)
                if attempt_no == 1:
                    continue  # ONE re-ask on invalid JSON
                return _hold_vote(model, extract_err, total_latency)

            self._log_call(model, decision_id, run_id, raw=content, parsed=parsed,
                           valid=True, tokens=tokens, latency_ms=latency_ms, error=None)
            return ModelVote(model=model, action=parsed["action"],
                             confidence=parsed["confidence"],
                             stop_pct=parsed["stop_pct"],
                             target_pct=parsed["target_pct"],
                             reason=parsed["reason"], valid=True,
                             latency_ms=total_latency)
        return _hold_vote(model, "unreachable", total_latency)  # pragma: no cover

    def decide_all(self, input_json: Dict[str, Any],
                   models: Optional[List[str]] = None,
                   decision_id: Optional[int] = None,
                   run_id: Optional[int] = None) -> List[ModelVote]:
        """One request per model, in parallel; votes in `models` order."""
        models = list(models if models is not None else self.cfg.llm.MODELS)
        if not models:
            return []
        if len(models) == 1:
            return [self._decide_one(models[0], input_json, decision_id, run_id)]
        with ThreadPoolExecutor(max_workers=len(models)) as pool:
            futures = [pool.submit(self._decide_one, m, input_json, decision_id, run_id)
                       for m in models]
            return [f.result() for f in futures]

    # -- single free-form ask (used by scripts/probe_llm.py) ------------------
    def ask(self, model: str, system: str, user: str,
            json_mode: Optional[bool] = None,
            timeout: Optional[float] = None) -> Dict[str, Any]:
        """Single non-voting call with ONE retry on timeout/5xx. Returns
        dict(ok, content, parsed, latency_ms, error, attempts)."""
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if self._json_mode_enabled(json_mode):
            payload["response_format"] = {"type": "json_object"}
        effective_timeout = float(timeout if timeout is not None
                                  else self.cfg.llm.per_model_timeout_sec)
        total_latency = 0.0
        for attempt_no in (1, 2):
            t0 = time.perf_counter()
            status, body, err = self._attempt(payload, effective_timeout)
            latency_ms = (time.perf_counter() - t0) * 1000
            total_latency += latency_ms
            if err is not None:
                if attempt_no == 1:
                    continue
                return {"ok": False, "content": None, "parsed": None,
                        "latency_ms": total_latency, "error": err, "attempts": attempt_no}
            if status != 200:
                err = f"http_{status}"
                if status >= 500 and attempt_no == 1:
                    continue
                return {"ok": False, "content": None, "parsed": None,
                        "latency_ms": total_latency, "error": err, "attempts": attempt_no}
            content, extract_err = _extract_content(body)
            if extract_err is None:
                try:
                    return {"ok": True, "content": content,
                            "parsed": parse_vote(content),
                            "latency_ms": total_latency, "error": None,
                            "attempts": attempt_no}
                except LlmError as exc:
                    extract_err = str(exc)
            return {"ok": False, "content": content, "parsed": None,
                    "latency_ms": total_latency, "error": extract_err,
                    "attempts": attempt_no}
        return {"ok": False, "content": None, "parsed": None,  # pragma: no cover
                "latency_ms": total_latency, "error": "unreachable", "attempts": 2}
