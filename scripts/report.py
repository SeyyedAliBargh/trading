#!/usr/bin/env python3
"""Read-only audit report for the trading agent (scripts/report.py).

Opens the SQLite database in read-only URI mode (file:...?mode=ro) so it can
run while the bot is running. Pure SQL + formatting; no writes, no network.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.request import pathname2url


def open_read_only(db_path: str) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.exists():
        raise SystemExit(f"ERROR: database not found: {path}")
    uri = f"file:{pathname2url(str(path))}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    cur = conn.execute(sql, params)
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.fetchall()]


def scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return row[0] if row else None


def fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)


def table(headers: List[str], data: List[List[Any]]) -> str:
    """Fixed-width ASCII table."""
    str_rows = [[fmt(v) for v in row] for row in data]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * w for w in widths)
    body = "\n".join("  ".join(c.ljust(widths[i]) for i, c in enumerate(r))
                     for r in str_rows)
    return "\n".join([line, sep] + ([body] if body else ["(none)"]))


def p95(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[idx]


def resolve_run(conn: sqlite3.Connection, run_arg: str) -> Optional[int]:
    if run_arg in (None, "", "latest"):
        return scalar(conn, "SELECT id FROM runs ORDER BY id DESC LIMIT 1")
    try:
        return int(run_arg)
    except ValueError:
        raise SystemExit(f"ERROR: --run must be 'latest' or an integer, got {run_arg!r}")


def since_clause(args: argparse.Namespace) -> tuple:
    """(clause_sql, params) filtering time-series rows to the last N hours."""
    if args.since_hours is None:
        return ("", ())
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.since_hours)).isoformat()
    return (" AND ts >= ? ", (cutoff,))


def report(conn: sqlite3.Connection, run_id: Optional[int],
           args: argparse.Namespace) -> str:
    out: List[str] = []
    add = out.append

    # ---- run info -------------------------------------------------------
    run_rows = rows(conn, "SELECT * FROM runs WHERE id = ?", (run_id,)) if run_id \
        else []
    run = run_rows[0] if run_rows else None
    if run is None:
        return "No runs found in the database."
    config_json = {}
    try:
        config_json = json.loads(run.get("config_json") or "{}")
    except (TypeError, ValueError):
        config_json = {}
    models = (config_json.get("llm") or {}).get("MODELS") or []
    prompt_versions = rows(
        conn, "SELECT DISTINCT prompt_version FROM decisions WHERE run_id = ?",
        (run_id,))
    add("=== RUN ===")
    add(f"run id:        {run['id']}")
    add(f"mode:          {run['mode']}")
    add(f"started:       {run['started_at']}")
    add(f"ended:         {fmt(run['ended_at'])}")
    add(f"prompt ver:    {', '.join(r['prompt_version'] or '?' for r in prompt_versions) or '-'}")
    add(f"models:        {', '.join(models) or '-'}")
    add("")

    # ---- equity -----------------------------------------------------------
    eq = rows(conn, "SELECT ts, total_value_tmn FROM balance_snapshots "
                    "WHERE run_id = ? ORDER BY ts", (run_id,))
    values = [float(r["total_value_tmn"]) for r in eq if r["total_value_tmn"] is not None]
    if values:
        first, last = values[0], values[-1]
        low = min(values)
        peak = values[0]
        max_dd = 0.0
        for v in values:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak * 100.0)
        ret_pct = (last - first) / first * 100.0 if first else 0.0
        add("=== EQUITY (balance_snapshots) ===")
        add(f"snapshots:     {len(values)}")
        add(f"first:         {first:,.0f} TMN")
        add(f"last:          {last:,.0f} TMN   ({ret_pct:+.2f}%)")
        add(f"min:           {low:,.0f} TMN")
        add(f"max drawdown:  {max_dd:.2f}%")
        add("")

    # ---- decisions ---------------------------------------------------------
    since_sql, since_params = since_clause(args)
    by_action = rows(conn, "SELECT final_action, COUNT(*) AS n FROM decisions "
                           f"WHERE run_id = ?{since_sql} GROUP BY final_action "
                           "ORDER BY n DESC", (run_id, *since_params))
    by_source = rows(conn, "SELECT source, COUNT(*) AS n FROM decisions "
                           f"WHERE run_id = ?{since_sql} GROUP BY source ORDER BY n DESC",
                     (run_id, *since_params))
    reached_guard = scalar(conn, "SELECT COUNT(*) FROM decisions "
                                 f"WHERE run_id = ?{since_sql} AND guard_allowed IS NOT NULL",
                           (run_id, *since_params))
    total_decisions = scalar(conn, f"SELECT COUNT(*) FROM decisions WHERE run_id = ?{since_sql}",
                             (run_id, *since_params))
    add("=== DECISIONS ===")
    add(f"total:         {total_decisions}")
    add("by action:")
    for r in by_action:
        add(f"  {fmt(r['final_action']):12s} {r['n']}")
    add("by source:")
    for r in by_source:
        add(f"  {fmt(r['source']):12s} {r['n']}")
    add(f"BUY votes that reached the guard: {reached_guard}")
    add("")

    # ---- guard rejections ----------------------------------------------------
    rejections = rows(conn, "SELECT rule, COUNT(*) AS n FROM risk_checks "
                            "WHERE passed = 0 AND decision_id IN "
                            "(SELECT id FROM decisions WHERE run_id = ?) "
                            "GROUP BY rule ORDER BY n DESC", (run_id,))
    total_rej = sum(r["n"] for r in rejections) or 1
    add("=== GUARD REJECTIONS BY RULE ===")
    data = []
    for r in rejections:
        pct = r["n"] / total_rej * 100.0
        label = f"{' <<<' if r['rule'] == 'net_edge' else ''}"
        data.append([f"{r['rule']}{label}", r["n"], f"{pct:.1f}%"])
    if data:
        add(table(["rule", "count", "pct"], data))
    else:
        add("(no rule rejections)")
    add("")

    # ---- trades (positions + their entry decisions) ---------------------------
    ts_filter = "AND p.opened_at >= ?" if args.since_hours is not None else ""
    ts_params = []
    if args.since_hours is not None:
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(hours=args.since_hours)).isoformat()
        ts_params.append(cutoff)
    trades = rows(conn, "SELECT p.*, d.decision_uid, d.votes_json, d.reason "
                        "FROM positions p LEFT JOIN decisions d "
                        "ON p.entry_decision_id = d.id "
                        f"WHERE p.run_id = ? {ts_filter} ORDER BY p.id",
                  (run_id, *ts_params))
    add("=== TRADES ===")
    for tr in trades:
        entry_value = float(tr.get("entry_value_tmn") or 0.0)
        pnl = tr.get("pnl_tmn")
        pnl_pct = (float(pnl) / entry_value * 100.0) if (pnl is not None and entry_value) else None
        add(f"[{tr['id']}] {tr['symbol']}  decision_uid={tr.get('decision_uid') or '-'}")
        add(f"    entry:  {fmt(tr['opened_at'])}  price={fmt(tr['entry_price'])}  "
            f"qty={fmt(tr['quantity'])}  value={fmt(entry_value)} TMN")
        add(f"    exit:   {fmt(tr.get('closed_at'))}  price={fmt(tr.get('close_price'))}  "
            f"reason={fmt(tr.get('close_reason'))}  status={tr['status']}")
        if pnl is not None:
            add(f"    pnl:    {float(pnl):,.1f} TMN"
                + (f"  ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""))
        votes = []
        try:
            votes = json.loads(tr.get("votes_json") or "[]")
        except (TypeError, ValueError):
            votes = []
        if votes:
            add("    entry votes:")
            for v in votes:
                add(f"      {fmt(v.get('model'))}: {fmt(v.get('action'))} "
                    f"conf={fmt(v.get('confidence'))} "
                    f"stop={fmt(v.get('stop_pct'))} target={fmt(v.get('target_pct'))}")
                reason = v.get("reason")
                if reason:
                    add(f"        reason: {str(reason)[:160]}")
                if v.get("error"):
                    add(f"        error: {str(v['error'])[:160]}")
        add("")

    # ---- LLM stats ----------------------------------------------------------
    llm_since = f" AND ts >= ?" if args.since_hours is not None else ""
    llm_params: List[Any] = [run_id]
    if args.since_hours is not None:
        llm_params.append((datetime.now(timezone.utc) -
                           timedelta(hours=args.since_hours)).isoformat())
    llm = rows(conn, "SELECT model, latency_ms, prompt_tokens, completion_tokens, "
                     "valid, error FROM llm_calls WHERE run_id = ?" + llm_since,
               tuple(llm_params))
    per_model: Dict[str, Dict[str, Any]] = {}
    for call in llm:
        m = call["model"] or "?"
        bucket = per_model.setdefault(m, {"n": 0, "errors": 0,
                                          "lat": [], "tokens": 0})
        bucket["n"] += 1
        if call["valid"] == 0 or call["error"]:
            bucket["errors"] += 1
        if call["latency_ms"] is not None:
            bucket["lat"].append(float(call["latency_ms"]))
        bucket["tokens"] += int(call["prompt_tokens"] or 0) + int(call["completion_tokens"] or 0)
    add("=== LLM CALLS ===")
    data = []
    for m, b in sorted(per_model.items()):
        avg = sum(b["lat"]) / len(b["lat"]) if b["lat"] else None
        data.append([m, b["n"], b["errors"],
                     f"{(b['errors'] / b['n'] * 100):.1f}%",
                     fmt(avg), fmt(p95(b["lat"])), b["tokens"]])
    if data:
        add(table(["model", "calls", "errors", "err%", "avg_ms", "p95_ms", "tokens"], data))
    else:
        add("(no llm calls)")
    add("")

    # ---- events -----------------------------------------------------------
    ev_since = " AND ts >= ?" if args.since_hours is not None else ""
    ev_params: List[Any] = [run_id]
    if args.since_hours is not None:
        ev_params.append((datetime.now(timezone.utc) -
                          timedelta(hours=args.since_hours)).isoformat())
    event_counts = rows(conn, "SELECT kind, COUNT(*) AS n FROM events "
                              "WHERE run_id = ?" + ev_since +
                              " GROUP BY kind ORDER BY n DESC", tuple(ev_params))
    kinds = {r["kind"]: r["n"] for r in event_counts}
    add("=== EVENTS ===")
    interesting = [("TRIGGER", "price triggers"),
                   ("ENTRY_UNFILLED", "unfilled entries"),
                   ("CLOSE_ATTEMPT", "close attempts"),
                   ("RATE_LIMIT", "429 backoffs"),
                   ("GUARDIAN_TICK_ERROR", "guardian exceptions"),
                   ("GUARDIAN_RESTART", "guardian restarts"),
                   ("CRITICAL", "CRITICAL")]
    for kind, label in interesting:
        add(f"  {kind:20s} {kinds.get(kind, 0):6d}  ({label})")
    add(f"  {'OTHER':20s} {sum(n for k, n in kinds.items() if k not in [i[0] for i in interesting]):6d}")
    add("")

    # ---- paper honesty ------------------------------------------------------
    if run["mode"] == "paper":
        add("NOTE: mode is PAPER (DRY_RUN). Paper results are optimistic: fills")
        add("happen at the book price with no queue position and no market impact.")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report.py", description="Read-only audit report for the trading agent")
    parser.add_argument("--db", default="runtime/agent.db",
                        help="Path to the SQLite database (default runtime/agent.db)")
    parser.add_argument("--run", default="latest",
                        help="Run id or 'latest' (default latest)")
    parser.add_argument("--since-hours", type=int, default=None,
                        help="Only count time-series rows from the last N hours")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    conn = open_read_only(args.db)
    try:
        run_id = resolve_run(conn, args.run)
        print(report(conn, run_id, args))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
