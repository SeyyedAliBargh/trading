"""Read-only dashboard backend (ui/server.py). STDLIB ONLY.

Serves JSON API endpoints and the single-file front-end (ui/index.html)
directly from SQLite, opened READ-ONLY per request via the URI
"file:<path>?mode=ro". The UI can therefore NEVER affect the bot:
- GET only (anything else -> 405),
- every handler is exception-guarded and answers JSON, never a traceback,
- its own daemon thread + ThreadingHTTPServer (never blocks trading),
- binds 127.0.0.1 by default.

Standalone:  python ui/server.py [--db PATH] [--log PATH] [--port 8765]
Embedded:    start_ui_thread(db_path, ...) -> handle with .stop()/.url,
             or None when the port is busy (never raises).
"""
import argparse
import csv
import io
import json
import logging
import re
import socket
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import pathname2url

INDEX_HTML = Path(__file__).resolve().parent / "index.html"
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
EQUITY_MAX_POINTS = 500
RAW_TEXT_CAP = 200_000  # display cap for one raw LLM response
LOGTAIL_MAX_LINES = 1000
SECRET_LINE_RE = re.compile(
    r"(?i)(api[-_]?key|authorization|bearer|x-api-key|secret|token)")


# ---------------------------------------------------------------------------
# read-only DB access
# ---------------------------------------------------------------------------
def _connect(db_path: str) -> sqlite3.Connection:
    """One read-only connection per request; raises when the DB is missing."""
    path = Path(db_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"database not found: {path}")
    uri = f"file:{pathname2url(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> List[Dict]:
    cur = conn.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def _scalar(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row is not None else None


def _parse_json(text: Any, default: Any) -> Any:
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default


def _resolve_run(conn: sqlite3.Connection, run_arg: str) -> Optional[int]:
    if run_arg in (None, "", "latest"):
        return _scalar(conn, "SELECT id FROM runs ORDER BY id DESC LIMIT 1")
    try:
        return int(run_arg)
    except (TypeError, ValueError):
        return None


def _since_filter(since_hours: Optional[str]) -> Tuple[str, Tuple]:
    """(sql fragment, params) for ts >= cutoff on time-series tables."""
    if since_hours is None:
        return ("", ())
    try:
        hours = float(since_hours)
    except (TypeError, ValueError):
        return ("", ())
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return (" AND ts >= ? ", (cutoff,))


def _age_seconds(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        then = datetime.fromisoformat(ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())
    except (TypeError, ValueError):
        return None


def _p95(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]


# ---------------------------------------------------------------------------
# endpoint payloads (each guarded by the caller; missing tables -> warning)
# ---------------------------------------------------------------------------
def _overview(conn: sqlite3.Connection, run_id: Optional[int]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"warning": None, "run_id": run_id}
    run = _rows(conn, "SELECT * FROM runs WHERE id = ?", (run_id,))[0] \
        if run_id is not None and _rows(conn, "SELECT id FROM runs WHERE id = ?",
                                        (run_id,)) else None
    if run is None:
        out["warning"] = "no runs found"
        out["mode"] = None
        return out
    out["mode"] = run.get("mode", "").upper()
    out["run_started_at"] = run.get("started_at")
    out["run_ended_at"] = run.get("ended_at")
    out["uptime_sec"] = _age_seconds(run.get("started_at"))
    config = _parse_json(run.get("config_json"), {})
    out["models"] = (config.get("llm") or {}).get("MODELS") or []
    out["symbols"] = (config.get("trading") or {}).get("SYMBOLS") or []
    out["prompt_version"] = _scalar(
        conn, "SELECT prompt_version FROM decisions WHERE run_id = ? "
        "AND prompt_version IS NOT NULL ORDER BY id DESC LIMIT 1", (run_id,))
    eq = [float(r["total_value_tmn"]) for r in _rows(
        conn, "SELECT total_value_tmn FROM balance_snapshots WHERE run_id = ? "
        "AND total_value_tmn IS NOT NULL ORDER BY ts", (run_id,))]
    if eq:
        out["equity_first"] = eq[0]
        out["equity_last"] = eq[-1]
        out["equity_min"] = min(eq)
        out["equity_max"] = max(eq)
        out["return_pct"] = ((eq[-1] - eq[0]) / eq[0] * 100.0) if eq[0] else None
        peak, dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak * 100.0)
        out["max_drawdown_pct"] = dd
    else:
        out["warning"] = "no balance snapshots yet"
    last_snap = _rows(conn, "SELECT tmn_balance, total_value_tmn, ts FROM "
                            "balance_snapshots WHERE run_id = ? ORDER BY id DESC "
                            "LIMIT 1", (run_id,))
    if last_snap:
        out["free_tmn"] = last_snap[0].get("tmn_balance")
        out["equity_ts"] = last_snap[0].get("ts")
    out["open_positions"] = _scalar(
        conn, "SELECT COUNT(*) FROM positions WHERE run_id = ? AND status = 'OPEN'",
        (run_id,))
    hb = _rows(conn, "SELECT ts FROM events WHERE kind = 'HEARTBEAT' "
                    "ORDER BY id DESC LIMIT 1")
    out["last_heartbeat_age_sec"] = _age_seconds(hb[0]["ts"]) if hb else None
    return out


def _equity(conn: sqlite3.Connection, run_id: Optional[int],
            since_hours: Optional[str]) -> Dict[str, Any]:
    frag, params = _since_filter(since_hours)
    rows = _rows(conn, "SELECT ts, total_value_tmn FROM balance_snapshots "
                       f"WHERE run_id = ?{frag} ORDER BY ts",
                 (run_id, *params))
    if len(rows) > EQUITY_MAX_POINTS:
        step = len(rows) / float(EQUITY_MAX_POINTS)
        rows = [rows[int(i * step)] for i in range(EQUITY_MAX_POINTS)]
    return {"points": [
        {"ts": r["ts"], "value": r["total_value_tmn"]} for r in rows],
        "warning": None if rows else "no balance snapshots yet"}


def _last_known_bid(conn: sqlite3.Connection, symbol: str) -> Tuple[Optional[float],
                                                                    Optional[str]]:
    """(bid, ts) from the latest GUARDIAN_WATCH event, else market_snapshots."""
    rows = _rows(conn, "SELECT ts, data_json FROM events "
                       "WHERE kind = 'GUARDIAN_WATCH' AND data_json LIKE ? "
                       "ORDER BY id DESC LIMIT 1", (f'%"{symbol}"%',))
    for r in rows:
        data = _parse_json(r.get("data_json"), {})
        if data.get("symbol") == symbol and data.get("best_bid") is not None:
            return float(data["best_bid"]), r.get("ts")
    snap = _rows(conn, "SELECT ts, best_bid FROM market_snapshots WHERE symbol = ? "
                      "AND best_bid IS NOT NULL ORDER BY id DESC LIMIT 1", (symbol,))
    if snap:
        return float(snap[0]["best_bid"]), snap[0]["ts"]
    return None, None


def _positions(conn: sqlite3.Connection, run_id: Optional[int]) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT * FROM positions WHERE run_id = ? AND status = 'OPEN' "
                       "ORDER BY id", (run_id,))
    out = []
    for p in rows:
        bid, bid_ts = _last_known_bid(conn, p["symbol"])
        entry = p.get("entry_price") or 0.0
        item = dict(p)
        item["last_known_bid"] = bid
        item["last_known_bid_ts"] = bid_ts
        if bid is not None and p.get("quantity"):
            item["unrealized_pnl_tmn"] = (bid - entry) * float(p["quantity"])
            item["unrealized_pnl_pct"] = ((bid - entry) / entry * 100.0) \
                if entry else None
        out.append(item)
    return {"positions": out,
            "warning": None if out else "no open positions"}


def _guardian(conn: sqlite3.Connection, run_id: Optional[int]) -> Dict[str, Any]:
    per_symbol: Dict[str, Dict[str, Any]] = {}
    for row in _rows(conn, "SELECT ts, data_json FROM events "
                           "WHERE kind = 'GUARDIAN_WATCH' ORDER BY id DESC"):
        data = _parse_json(row.get("data_json"), {})
        symbol = data.get("symbol")
        if symbol and symbol not in per_symbol:
            data["age_sec"] = _age_seconds(row.get("ts"))
            data["ts"] = row.get("ts")
            per_symbol[symbol] = data
    heartbeat = _rows(conn, "SELECT ts FROM events WHERE kind = 'HEARTBEAT' "
                            "ORDER BY id DESC LIMIT 1")
    triggers = _rows(conn, "SELECT ts, data_json, message FROM events "
                           "WHERE kind = 'TRIGGER' ORDER BY id DESC LIMIT 10")
    return {
        "symbols": per_symbol,
        "last_heartbeat_age_sec": _age_seconds(heartbeat[0]["ts"]) if heartbeat else None,
        "last_triggers": [{"ts": t["ts"], "message": t["message"],
                           "data": _parse_json(t.get("data_json"), {})}
                          for t in triggers],
        "warning": None if per_symbol else "no GUARDIAN_WATCH events yet "
                    "(guardian not running or older build)",
    }


def _decisions(conn: sqlite3.Connection, run_id: Optional[int],
               since_hours: Optional[str], limit: int, symbol: Optional[str],
               action: Optional[str], source: Optional[str]) -> Dict[str, Any]:
    frag, params = _since_filter(since_hours)
    sql = "SELECT * FROM decisions WHERE run_id = ?" + frag
    args: List[Any] = [run_id, *params]
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol)
    if action:
        sql += " AND final_action = ?"
        args.append(action.upper())
    if source:
        sql += " AND source = ?"
        args.append(source.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    rows = _rows(conn, sql, tuple(args))
    items = []
    for r in rows:
        item = dict(r)
        item["votes"] = _parse_json(r.get("votes_json"), [])
        items.append(item)
    return {"decisions": items, "warning": None if items else "no decisions"}


def _decision_detail(conn: sqlite3.Connection, decision_id: int) -> Dict[str, Any]:
    row = _rows(conn, "SELECT * FROM decisions WHERE id = ?", (decision_id,))
    if not row:
        return {"error": f"decision {decision_id} not found", "warning": None}
    dec = dict(row[0])
    dec["votes"] = _parse_json(dec.get("votes_json"), [])
    models = []
    for call in _rows(conn, "SELECT * FROM llm_calls WHERE decision_id = ? "
                            "ORDER BY id", (decision_id,)):
        raw = call.get("raw_output") or ""
        models.append({
            "model": call.get("model"),
            "raw": raw[:RAW_TEXT_CAP],
            "raw_truncated": len(raw) > RAW_TEXT_CAP,
            "parsed": _parse_json(call.get("parsed_json"), None),
            "valid": call.get("valid"),
            "latency_ms": call.get("latency_ms"),
            "prompt_tokens": call.get("prompt_tokens"),
            "completion_tokens": call.get("completion_tokens"),
            "error": call.get("error"),
        })
    uid = dec.get("decision_uid")
    linked_events: List[Dict[str, Any]] = []
    if uid:
        linked_events = _rows(
            conn, "SELECT id, ts, level, kind, message, data_json FROM events "
            "WHERE data_json LIKE ? ORDER BY id DESC LIMIT 50", (f"%{uid}%",))
    return {
        "decision": dec,
        "models": models,
        "guard_rules": _rows(conn, "SELECT rule, passed, value, threshold, message "
                                   "FROM risk_checks WHERE decision_id = ? ORDER BY id",
                             (decision_id,)),
        "orders": _rows(conn, "SELECT * FROM orders WHERE decision_id = ? ORDER BY id",
                        (decision_id,)),
        "events": linked_events,
        "position": _rows(conn, "SELECT * FROM positions WHERE entry_decision_id = ?",
                          (decision_id,)),
        "warning": None,
    }


def _trades(conn: sqlite3.Connection, run_id: Optional[int]) -> Dict[str, Any]:
    rows = _rows(conn, "SELECT p.*, d.decision_uid, d.votes_json FROM positions p "
                       "LEFT JOIN decisions d ON p.entry_decision_id = d.id "
                       "WHERE p.run_id = ? ORDER BY p.id DESC", (run_id,))
    out = []
    for p in rows:
        item = dict(p)
        entry_value = float(p.get("entry_value_tmn") or 0.0)
        pnl = p.get("pnl_tmn")
        item["pnl_pct"] = (float(pnl) / entry_value * 100.0) \
            if (pnl is not None and entry_value) else None
        votes = _parse_json(p.get("votes_json"), [])
        item["entry_votes"] = [{"model": v.get("model"), "action": v.get("action"),
                                "confidence": v.get("confidence"),
                                "reason": v.get("reason")} for v in votes]
        out.append(item)
    return {"trades": out, "warning": None if out else "no trades yet"}


def _guard(conn: sqlite3.Connection, run_id: Optional[int]) -> Dict[str, Any]:
    rejections = _rows(conn, "SELECT rule, COUNT(*) AS n FROM risk_checks "
                             "WHERE passed = 0 AND decision_id IN "
                             "(SELECT id FROM decisions WHERE run_id = ?) "
                             "GROUP BY rule ORDER BY n DESC", (run_id,))
    total = sum(r["n"] for r in rejections) or 1
    return {"rejections": [
        {"rule": r["rule"], "count": r["n"],
         "pct": round(r["n"] / total * 100.0, 1),
         "highlight": r["rule"] == "net_edge"} for r in rejections],
        "warning": None if rejections else "no rule rejections"}


def _llm(conn: sqlite3.Connection, run_id: Optional[int],
         since_hours: Optional[str]) -> Dict[str, Any]:
    frag, params = _since_filter(since_hours)
    rows = _rows(conn, "SELECT model, latency_ms, prompt_tokens, completion_tokens, "
                       f"valid, error FROM llm_calls WHERE run_id = ?{frag}",
                 (run_id, *params))
    per: Dict[str, Dict[str, Any]] = {}
    for c in rows:
        m = c.get("model") or "?"
        b = per.setdefault(m, {"calls": 0, "errors": 0, "lat": [], "tokens": 0})
        b["calls"] += 1
        if c.get("valid") == 0 or c.get("error"):
            b["errors"] += 1
        if c.get("latency_ms") is not None:
            b["lat"].append(float(c["latency_ms"]))
        b["tokens"] += int(c.get("prompt_tokens") or 0) + \
            int(c.get("completion_tokens") or 0)
    stats = []
    for m, b in sorted(per.items()):
        avg = sum(b["lat"]) / len(b["lat"]) if b["lat"] else None
        stats.append({"model": m, "calls": b["calls"], "errors": b["errors"],
                      "error_rate": round(b["errors"] / b["calls"], 3),
                      "avg_latency_ms": avg, "p95_latency_ms": _p95(b["lat"]),
                      "tokens": b["tokens"]})
    return {"models": stats, "warning": None if stats else "no llm calls"}


def _events(conn: sqlite3.Connection, run_id: Optional[int], limit: int,
            kind: Optional[str]) -> Dict[str, Any]:
    sql = "SELECT * FROM events WHERE run_id = ?"
    args: List[Any] = [run_id]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    rows = _rows(conn, sql, tuple(args))
    return {"events": rows, "warning": None if rows else "no events"}


def _logtail(log_path: Optional[str], lines: int) -> Dict[str, Any]:
    if not log_path:
        return {"lines": [], "warning": "log tail not configured"}
    path = Path(log_path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"lines": [], "warning": f"log not readable: {exc}"}
    all_lines = text.splitlines()
    tail = all_lines[-max(1, min(int(lines), LOGTAIL_MAX_LINES)):]
    safe = []
    for ln in tail:
        if SECRET_LINE_RE.search(ln):
            safe.append("[redacted: possible secret]")
        else:
            safe.append(ln)
    return {"lines": safe, "warning": None if safe else "log is empty"}


def _all(conn: sqlite3.Connection, run_id: Optional[int],
         since_hours: Optional[str]) -> Dict[str, Any]:
    return {
        "overview": _overview(conn, run_id),
        "equity": _equity(conn, run_id, since_hours),
        "positions": _positions(conn, run_id),
        "guardian": _guardian(conn, run_id),
        "decisions": _decisions(conn, run_id, since_hours, 50, None, None, None),
        "trades": _trades(conn, run_id),
        "guard": _guard(conn, run_id),
        "llm": _llm(conn, run_id, since_hours),
        "events": _events(conn, run_id, 200, None),
    }


def _report_text(conn: sqlite3.Connection, run_id: Optional[int]) -> str:
    """Compact text audit mirroring scripts/report.py (self-contained copy)."""
    lines: List[str] = []
    ov = _overview(conn, run_id)
    lines.append("=== RUN ===")
    lines.append(f"run id: {ov.get('run_id')}  mode: {ov.get('mode') or '-'}")
    lines.append(f"started: {ov.get('run_started_at')}  ended: {ov.get('run_ended_at')}")
    lines.append(f"prompt: {ov.get('prompt_version')}  models: {', '.join(ov.get('models') or [])}")
    if "equity_first" in ov:
        lines.append(f"equity: {ov['equity_first']:,.0f} -> {ov['equity_last']:,.0f} "
                     f"({ov.get('return_pct'):+.2f}%)  max dd {ov['max_drawdown_pct']:.2f}%")
    dec = _rows(conn, "SELECT final_action, COUNT(*) AS n FROM decisions "
                      "WHERE run_id = ? GROUP BY final_action", (run_id,))
    lines.append("=== DECISIONS ===")
    for r in dec:
        lines.append(f"  {r['final_action']}: {r['n']}")
    lines.append("=== GUARD REJECTIONS ===")
    g = _guard(conn, run_id)
    for r in g["rejections"]:
        mark = " <<<" if r["highlight"] else ""
        lines.append(f"  {r['rule']}{mark}: {r['count']} ({r['pct']}%)")
    if not g["rejections"]:
        lines.append("  (none)")
    lines.append("=== TRADES ===")
    for t in _trades(conn, run_id)["trades"]:
        lines.append(f"  [{t['id']}] {t['symbol']} entry={t['entry_price']} "
                     f"qty={t['quantity']} status={t['status']} "
                     f"reason={t.get('close_reason')} pnl={t.get('pnl_tmn')}")
    lines.append("=== LLM ===")
    for m in _llm(conn, run_id, None)["models"]:
        lines.append(f"  {m['model']}: {m['calls']} calls, "
                      f"{m['error_rate'] * 100:.1f}% errors, p95={m['p95_latency_ms']}")
    lines.append("=== EVENTS ===")
    for e in _events(conn, run_id, 50, None)["events"]:
        lines.append(f"  {e['ts']} {e['level']} {e['kind']}: {e['message']}")
    if ov.get("mode") == "PAPER":
        lines.append("NOTE: PAPER (DRY_RUN) results are optimistic: fills at the "
                     "book price, no queue position, no market impact.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# exports
# ---------------------------------------------------------------------------
def _csv_response(rows: List[Dict[str, Any]]) -> Tuple[bytes, str]:
    if not rows:
        return b"", "text/csv"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for row in rows:
        writer.writerow({k: (json.dumps(v, ensure_ascii=False)
                             if isinstance(v, (dict, list)) else v)
                         for k, v in row.items()})
    return buf.getvalue().encode("utf-8"), "text/csv"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
def _make_handler(db_path: str, log_path: Optional[str],
                  logger: Optional[Any]) -> type:
    class UiRequestHandler(BaseHTTPRequestHandler):
        # quiet, generic server string; no request logging to the console
        server_version = "TaskForceAgentUI"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            if logger is not None:
                try:
                    logger.debug("ui: " + (fmt % args))
                except Exception:
                    pass

        # -- helpers -----------------------------------------------------
        def _send(self, payload: bytes, content_type: str, status: int = 200,
                 download_name: Optional[str] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            if download_name:
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{download_name}"')
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, obj: Any, status: int = 200) -> None:
            try:
                payload = json.dumps(obj, ensure_ascii=False,
                                     default=str).encode("utf-8")
            except Exception:
                payload = json.dumps({"error": "serialization failed",
                                       "warning": None}).encode("utf-8")
                status = 500
            self._send(payload, "application/json; charset=utf-8", status)

        def _send_error_json(self, exc: BaseException) -> None:
            self._send_json({"error": f"{type(exc).__name__}: {exc}",
                             "warning": "see the bot log; the dashboard is "
                                        "read-only and cannot fix this"}, 500)

        def _query(self) -> Dict[str, List[str]]:
            try:
                return parse_qs(urlparse(self.path).query)
            except Exception:
                return {}

        def _one(self, key: str, default: Optional[str] = None) -> Optional[str]:
            values = self._query().get(key)
            return values[0] if values else default

        def _reject_405(self) -> None:
            self._send_json({"error": "method not allowed (GET only)",
                             "warning": None}, 405)

        # -- methods ------------------------------------------------------
        def do_GET(self):  # noqa: N802 (http.server naming)
            try:
                self._route_get()
            except BrokenPipeError:
                pass
            except Exception as exc:
                try:
                    self._send_error_json(exc)
                except Exception:
                    pass

        def do_POST(self):  # noqa: N802
            self._reject_405()

        def do_PUT(self):  # noqa: N802
            self._reject_405()

        def do_DELETE(self):  # noqa: N802
            self._reject_405()

        def do_PATCH(self):  # noqa: N802
            self._reject_405()

        def do_HEAD(self):  # noqa: N802
            self._reject_405()

        # -- routing -------------------------------------------------------
        def _route_get(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path in ("/style.css", "/app.js"):
                return self._serve_static(path)
            if not path.startswith("/api/"):
                return self._send_json({"error": "not found",
                                        "warning": None}, 404)
            try:
                conn = _connect(db_path)
            except Exception as exc:
                return self._send_json({"error": str(exc), "warning": None}, 200)
            try:
                self._route_api(conn, path)
            finally:
                conn.close()

        def _serve_index(self) -> None:
            try:
                payload = INDEX_HTML.read_bytes()
                self._send(payload, "text/html; charset=utf-8")
            except OSError as exc:
                self._send_json({"error": f"index.html missing: {exc}",
                                 "warning": None}, 404)

        def _serve_static(self, path: str) -> None:
            asset = INDEX_HTML.parent / path.lstrip("/")
            content_type = "text/css; charset=utf-8" if path.endswith(".css") \
                else "application/javascript; charset=utf-8"
            try:
                self._send(asset.read_bytes(), content_type)
            except OSError as exc:
                self._send_json({"error": f"asset missing: {exc}",
                                 "warning": None}, 404)

        def _route_api(self, conn: sqlite3.Connection, path: str) -> None:
            run_arg = self._one("run", "latest")
            since = self._one("since_hours")
            run_id: Optional[int] = None
            try:
                run_id = _resolve_run(conn, run_arg)
            except sqlite3.Error:
                run_id = None
            if run_id is None and run_arg not in (None, "", "latest"):
                return self._send_json({"error": "run not found",
                                        "warning": None}, 200)

            def _int(name: str, default: int) -> int:
                try:
                    return int(self._one(name, str(default)))
                except (TypeError, ValueError):
                    return default

            if path == "/api/overview":
                return self._send_json(_overview(conn, run_id))
            if path == "/api/equity":
                return self._send_json(_equity(conn, run_id, since))
            if path == "/api/positions":
                return self._send_json(_positions(conn, run_id))
            if path == "/api/guardian":
                return self._send_json(_guardian(conn, run_id))
            if path == "/api/decisions":
                return self._send_json(_decisions(
                    conn, run_id, since, _int("limit", 50),
                    self._one("symbol"), self._one("action"), self._one("source")))
            if path.startswith("/api/decision/"):
                try:
                    decision_id = int(path.rsplit("/", 1)[1])
                except ValueError:
                    return self._send_json({"error": "bad decision id",
                                            "warning": None}, 200)
                return self._send_json(_decision_detail(conn, decision_id))
            if path == "/api/trades":
                return self._send_json(_trades(conn, run_id))
            if path == "/api/guard":
                return self._send_json(_guard(conn, run_id))
            if path == "/api/llm":
                return self._send_json(_llm(conn, run_id, since))
            if path == "/api/events":
                return self._send_json(_events(conn, run_id, _int("limit", 200),
                                                self._one("kind")))
            if path == "/api/logtail":
                return self._send_json(_logtail(log_path, _int("lines", 100)))
            if path == "/api/export/decisions.csv":
                rows = _rows(conn, "SELECT * FROM decisions WHERE run_id = ? "
                                    "ORDER BY id", (run_id,))
                payload, ctype = _csv_response(rows)
                return self._send(payload, ctype, download_name="decisions.csv")
            if path == "/api/export/trades.csv":
                rows = _trades(conn, run_id)["trades"]
                payload, ctype = _csv_response(rows)
                return self._send(payload, ctype, download_name="trades.csv")
            if path == "/api/export/events.csv":
                rows = _rows(conn, "SELECT * FROM events WHERE run_id = ? "
                                   "ORDER BY id", (run_id,))
                payload, ctype = _csv_response(rows)
                return self._send(payload, ctype, download_name="events.csv")
            if path == "/api/export/report.txt":
                text = _report_text(conn, run_id)
                return self._send(text.encode("utf-8"), "text/plain; charset=utf-8",
                                  download_name="report.txt")
            if path == "/api/export/all.json":
                return self._send_json(_all(conn, run_id, since))
            return self._send_json({"error": "not found", "warning": None}, 404)

    return UiRequestHandler


# ---------------------------------------------------------------------------
# embedding + standalone
# ---------------------------------------------------------------------------
class _UiHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to share its port.

    On Windows SO_REUSEADDR would let a SECOND dashboard bind the same port
    silently; SO_EXCLUSIVEADDRUSE makes the bind fail so start_ui_thread can
    report the port as in use (POSIX already refuses the double bind)."""

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            try:
                self.socket.setsockopt(socket.SOL_SOCKET,
                                       socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
            # REUSEADDR + EXCLUSIVEADDRUSE together fail with WSAEINVAL
            self.allow_reuse_address = False
        super().server_bind()

    def handle_error(self, request, client_address):  # never print tracebacks
        pass


class UiHandle:
    """Handle for a running UI thread: .stop() and .url; never raises."""

    def __init__(self, server: ThreadingHTTPServer, thread: threading.Thread,
                 url: str):
        self._server = server
        self._thread = thread
        self.url = url
        self._stopped = threading.Event()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        try:
            self._thread.join(timeout=5.0)
        except Exception:
            pass

    @property
    def is_running(self) -> bool:
        return not self._stopped.is_set()


def start_ui_thread(db_path: str, host: str = DEFAULT_HOST,
                    port: int = DEFAULT_PORT, logger: Optional[Any] = None,
                    log_path: Optional[str] = None) -> Optional[UiHandle]:
    """Start the dashboard on a daemon thread. Returns a handle with .url and
    .stop(), or None when the port is in use (OSError caught, never raises)."""
    try:
        server = _UiHTTPServer(
            (host, int(port)),
            _make_handler(str(db_path), log_path, logger))
    except OSError as exc:
        message = f"dashboard UI not started (port {port} on {host}: {exc})"
        try:
            if logger is not None:
                logger.warning(message)
            else:
                logging.getLogger(__name__).warning(message)
        except Exception:
            pass
        return None
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.25},
                              name="ui-server", daemon=True)
    thread.start()
    bound_host, bound_port = server.server_address[:2]
    return UiHandle(server, thread, f"http://{bound_host}:{bound_port}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ui/server.py",
        description="Read-only monitoring dashboard (own product; unofficial)")
    parser.add_argument("--db", default="runtime/agent.db",
                        help="SQLite database path (default runtime/agent.db)")
    parser.add_argument("--log", default=None,
                        help="Bot log file path for /api/logtail")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port to bind (default {DEFAULT_PORT})")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"Bind address (default {DEFAULT_HOST})")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    handle = start_ui_thread(args.db, args.host, args.port,
                             logging.getLogger("ui"), args.log)
    if handle is None:
        print("UI failed to start (port in use?)", file=sys.stderr)
        return 1
    print(f"Dashboard: {handle.url}  (Ctrl+C to stop)")
    try:
        threading.Event().wait()  # serve until interrupted
    except KeyboardInterrupt:
        pass
    finally:
        handle.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
