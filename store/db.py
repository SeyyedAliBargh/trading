"""SQLite state store for the trading agent (AGENTS.md "Database" section).

Design
------
- One shared connection (check_same_thread=False) guarded by a threading.RLock.
- WAL journal mode. The database file lives at config.CONFIG.storage.DB_PATH
  (runtime/agent.db by default); the directory is created if missing.
- Foreign-key-free plain integer id links; raw payloads stored as JSON text.
- ts columns are ISO 8601 UTC strings (microsecond precision, "+00:00" offset)
  set by this module. The identical format everywhere makes plain SQL string
  comparisons (e.g. closed_at >= ?) correct.

Error contract
--------------
Every write/update is NON-RAISING: on any failure it prints one
"[store.db] ... failed: ..." line to stderr and returns None (inserts and
log_event) or False (updates, close_position, end_run, paper-account setters).

Callers of STATE-CRITICAL writes (insert_order, insert_position,
update_position, close_position, set_paper_account) MUST check the returned
value and treat None/False as an error: the trading cycle must then become
HOLD and an ERROR event must be attempted. Log-only writes (log_event,
api_calls, market_snapshots, llm_calls, risk_checks, balance_snapshots,
decisions, start_run) may ignore the result.

Reads raise if the store is not initialized (call init_db() first); other
read errors propagate to the caller ("reads may raise"). The one exception is
get_paper_account, which performs a create-on-first-use write and therefore
never raises (it returns None on failure).
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config

# Shared connection state (guarded by _lock for every operation).
_conn: sqlite3.Connection | None = None
_db_path: str | None = None
_lock = threading.RLock()

# Columns that may be set through the update_* functions
# (id and run_id are immutable; *_json values may be dicts).
_DECISION_FIELDS = frozenset({
    "prompt_version", "source", "decision_uid", "input_json", "votes_json",
    "final_action", "confidence", "stop_pct", "target_pct", "reason",
    "guard_allowed", "outcome", "order_id", "position_id",
})
_ORDER_FIELDS = frozenset({
    "decision_id", "position_id", "client_id", "exchange_order_id", "symbol",
    "side", "type", "price", "quantity", "status", "filled_quantity",
    "avg_fill_price", "fee_tmn", "dry_run", "request_json", "response_json",
})
_POSITION_FIELDS = frozenset({
    "symbol", "status", "opened_at", "closed_at", "entry_price", "quantity",
    "entry_value_tmn", "stop_pct", "target_pct", "stop_price", "target_price",
    "close_price", "close_reason", "pnl_tmn", "fees_tmn", "dry_run",
    "entry_decision_id", "entry_order_id", "exit_order_id",
})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    git_commit TEXT,
    config_json TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT NOT NULL,
    service TEXT,
    method TEXT,
    path TEXT,
    request_json TEXT,
    status_code INTEGER,
    response_text TEXT,
    latency_ms REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT NOT NULL,
    symbol TEXT,
    best_bid REAL,
    best_ask REAL,
    last_price REAL,
    spread_pct REAL,
    depth_json TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT NOT NULL,
    symbol TEXT,
    prompt_version TEXT,
    source TEXT,
    decision_uid TEXT,
    input_json TEXT,
    votes_json TEXT,
    final_action TEXT,
    confidence REAL,
    stop_pct REAL,
    target_pct REAL,
    reason TEXT,
    guard_allowed INTEGER,
    outcome TEXT,
    order_id INTEGER,
    position_id INTEGER
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    decision_id INTEGER,
    ts TEXT NOT NULL,
    model TEXT,
    raw_output TEXT,
    parsed_json TEXT,
    valid INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    latency_ms REAL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS risk_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    decision_id INTEGER,
    ts TEXT NOT NULL,
    rule TEXT,
    passed INTEGER,
    value REAL,
    threshold REAL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decision_id INTEGER,
    position_id INTEGER,
    client_id TEXT UNIQUE,
    exchange_order_id TEXT,
    symbol TEXT,
    side TEXT,
    type TEXT,
    price REAL,
    quantity REAL,
    status TEXT,
    filled_quantity REAL,
    avg_fill_price REAL,
    fee_tmn REAL,
    dry_run INTEGER,
    request_json TEXT,
    response_json TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    symbol TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    entry_price REAL,
    quantity REAL,
    entry_value_tmn REAL,
    stop_pct REAL,
    target_pct REAL,
    stop_price REAL,
    target_price REAL,
    close_price REAL,
    close_reason TEXT
        CHECK (close_reason IS NULL OR
               close_reason IN ('STOP_LOSS', 'TAKE_PROFIT', 'MODEL_SELL', 'MANUAL',
                                'SHUTDOWN', 'DUST', 'EXTERNAL')),
    pnl_tmn REAL,
    fees_tmn REAL,
    dry_run INTEGER,
    entry_decision_id INTEGER,
    entry_order_id INTEGER,
    exit_order_id INTEGER
);

CREATE TABLE IF NOT EXISTS balance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT NOT NULL,
    mode TEXT CHECK (mode IN ('paper', 'live')),
    tmn_balance REAL,
    assets_json TEXT,
    total_value_tmn REAL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    ts TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('INFO', 'WARN', 'ERROR')),
    kind TEXT,
    message TEXT,
    data_json TEXT
);

CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    tmn_balance REAL,
    assets_json TEXT,
    initial_capital_tmn REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Current UTC time as a fixed-precision ISO 8601 string (sortable)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: Any) -> Any:
    """Prepare a value for a *_json column: None/str pass through, others
    are JSON-encoded; non-serializable values degrade to str(value)."""
    if value is None or isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _parse_json_text(text: Any) -> Any:
    """Parse a stored JSON text back into a Python value; empty -> {}."""
    if text is None or text == "":
        return {}
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _require_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("store.db is not initialized; call init_db() first")
    return _conn


def _git_short_commit() -> str:
    """Short commit hash of the current checkout, or "unknown"."""
    try:
        root = Path(__file__).resolve().parent.parent
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, cwd=root,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _insert(op: str, sql: str, params: tuple) -> int | None:
    """Run an INSERT under the lock; return the new row id or None."""
    try:
        conn = _require_conn()
        with _lock:
            cur = conn.execute(sql, params)
            conn.commit()
            return int(cur.lastrowid)
    except Exception as exc:
        print(f"[store.db] {op} failed: {exc}", file=sys.stderr)
        return None


def _update(op: str, sql: str, params: tuple) -> bool:
    """Run an UPDATE under the lock; return True on success, False on failure."""
    try:
        conn = _require_conn()
        with _lock:
            conn.execute(sql, params)
            conn.commit()
        return True
    except Exception as exc:
        print(f"[store.db] {op} failed: {exc}", file=sys.stderr)
        return False


def _apply_update(op: str, table: str, allowed: frozenset, row_id: int,
                  fields: dict, touch: str | None = None) -> bool:
    """Generic guarded column update; unknown fields fail with stderr."""
    if not fields:
        print(f"[store.db] {op} failed: no fields given", file=sys.stderr)
        return False
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            print(f"[store.db] {op} failed: unknown field '{key}'", file=sys.stderr)
            return False
        sets.append(f"{key} = ?")
        params.append(_json(value) if key.endswith("_json") else value)
    if touch is not None:
        sets.append(f"{touch} = ?")
        params.append(_utcnow_iso())
    params.append(row_id)
    return _update(op, f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", tuple(params))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def init_db(path: str | os.PathLike | None = None) -> None:
    """Open (or re-open) the store and create all tables if missing.

    path=None uses config.CONFIG.storage.DB_PATH. Calling init_db again is
    safe: the previous connection is closed first and the data is kept.
    """
    global _conn, _db_path
    target = str(path) if path is not None else config.CONFIG.storage.DB_PATH
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
        conn = sqlite3.connect(target, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        _conn = conn
        _db_path = target


def close_db() -> None:
    """Close the shared connection; safe to call when already closed."""
    global _conn, _db_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception as exc:
                print(f"[store.db] close_db failed: {exc}", file=sys.stderr)
            _conn = None
            _db_path = None


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def start_run(mode: str, config_snapshot: Any, git_commit: str | None = None,
              notes: str | None = None) -> int | None:
    """Record a new run and return its id (or None on failure)."""
    commit = git_commit if git_commit is not None else _git_short_commit()
    return _insert(
        "start_run",
        "INSERT INTO runs (started_at, ended_at, mode, git_commit, config_json, notes) "
        "VALUES (?, NULL, ?, ?, ?, ?)",
        (_utcnow_iso(), mode, commit, _json(config_snapshot), notes),
    )


def end_run(run_id: int) -> bool:
    """Stamp ended_at on the run; True on success, False on failure."""
    return _update("end_run", "UPDATE runs SET ended_at = ? WHERE id = ?",
                   (_utcnow_iso(), run_id))


# ---------------------------------------------------------------------------
# Inserts (one per table; return the new row id or None)
# ---------------------------------------------------------------------------


def insert_api_call(run_id: int | None, service: str | None = None, method: str | None = None,
                    path: str | None = None, request_json: Any = None,
                    status_code: int | None = None, response_text: str | None = None,
                    latency_ms: float | None = None, error: str | None = None) -> int | None:
    return _insert(
        "insert_api_call",
        "INSERT INTO api_calls (run_id, ts, service, method, path, request_json, "
        "status_code, response_text, latency_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, _utcnow_iso(), service, method, path, _json(request_json),
         status_code, response_text, latency_ms, error),
    )


def insert_market_snapshot(run_id: int | None, symbol: str | None = None,
                           best_bid: float | None = None, best_ask: float | None = None,
                           last_price: float | None = None, spread_pct: float | None = None,
                           depth: Any = None) -> int | None:
    return _insert(
        "insert_market_snapshot",
        "INSERT INTO market_snapshots (run_id, ts, symbol, best_bid, best_ask, "
        "last_price, spread_pct, depth_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, _utcnow_iso(), symbol, best_bid, best_ask, last_price, spread_pct, _json(depth)),
    )


def insert_decision(run_id: int | None, symbol: str | None = None,
                    prompt_version: str | None = None, input_json: Any = None,
                    votes_json: Any = None, final_action: str | None = None,
                    confidence: float | None = None, stop_pct: float | None = None,
                    target_pct: float | None = None, reason: str | None = None,
                    guard_allowed: bool | None = None, outcome: str | None = None,
                    order_id: int | None = None, position_id: int | None = None,
                    source: str | None = None,
                    decision_uid: str | None = None) -> int | None:
    return _insert(
        "insert_decision",
        "INSERT INTO decisions (run_id, ts, symbol, prompt_version, source, "
        "decision_uid, input_json, votes_json, final_action, confidence, stop_pct, "
        "target_pct, reason, guard_allowed, outcome, order_id, position_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, _utcnow_iso(), symbol, prompt_version, source, decision_uid,
         _json(input_json), _json(votes_json), final_action, confidence,
         stop_pct, target_pct, reason, guard_allowed, outcome, order_id, position_id),
    )


def insert_llm_call(run_id: int | None, decision_id: int | None = None,
                    model: str | None = None, raw_output: str | None = None,
                    parsed_json: Any = None, valid: bool | None = None,
                    prompt_tokens: int | None = None, completion_tokens: int | None = None,
                    latency_ms: float | None = None, error: str | None = None) -> int | None:
    return _insert(
        "insert_llm_call",
        "INSERT INTO llm_calls (run_id, decision_id, ts, model, raw_output, parsed_json, "
        "valid, prompt_tokens, completion_tokens, latency_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, decision_id, _utcnow_iso(), model, raw_output, _json(parsed_json), valid,
         prompt_tokens, completion_tokens, latency_ms, error),
    )


def insert_risk_check(run_id: int | None, decision_id: int | None = None,
                      rule: str | None = None, passed: bool | None = None,
                      value: float | None = None, threshold: float | None = None,
                      message: str | None = None) -> int | None:
    return _insert(
        "insert_risk_check",
        "INSERT INTO risk_checks (run_id, decision_id, ts, rule, passed, value, "
        "threshold, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, decision_id, _utcnow_iso(), rule, passed, value, threshold, message),
    )


def insert_order(run_id: int | None, decision_id: int | None = None,
                 position_id: int | None = None, client_id: str | None = None,
                 exchange_order_id: str | None = None, symbol: str | None = None,
                 side: str | None = None, type: str | None = None,
                 price: float | None = None, quantity: float | None = None,
                 status: str | None = None, filled_quantity: float | None = None,
                 avg_fill_price: float | None = None, fee_tmn: float | None = None,
                 dry_run: bool | None = None, request_json: Any = None,
                 response_json: Any = None) -> int | None:
    now = _utcnow_iso()
    return _insert(
        "insert_order",
        "INSERT INTO orders (run_id, ts, updated_at, decision_id, position_id, client_id, "
        "exchange_order_id, symbol, side, type, price, quantity, status, filled_quantity, "
        "avg_fill_price, fee_tmn, dry_run, request_json, response_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, now, now, decision_id, position_id, client_id, exchange_order_id,
         symbol, side, type, price, quantity, status, filled_quantity,
         avg_fill_price, fee_tmn, dry_run, _json(request_json), _json(response_json)),
    )


def insert_position(run_id: int | None, symbol: str | None = None,
                    entry_price: float | None = None, quantity: float | None = None,
                    entry_value_tmn: float | None = None, stop_pct: float | None = None,
                    target_pct: float | None = None, stop_price: float | None = None,
                    target_price: float | None = None, dry_run: bool | None = None,
                    entry_decision_id: int | None = None,
                    entry_order_id: int | None = None) -> int | None:
    """Open a new position row (status 'OPEN', opened_at set by this function)."""
    return _insert(
        "insert_position",
        "INSERT INTO positions (run_id, symbol, status, opened_at, entry_price, quantity, "
        "entry_value_tmn, stop_pct, target_pct, stop_price, target_price, dry_run, "
        "entry_decision_id, entry_order_id) VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, symbol, _utcnow_iso(), entry_price, quantity, entry_value_tmn,
         stop_pct, target_pct, stop_price, target_price, dry_run,
         entry_decision_id, entry_order_id),
    )


def insert_balance_snapshot(run_id: int | None, mode: str | None = None,
                            tmn_balance: float | None = None, assets: Any = None,
                            total_value_tmn: float | None = None) -> int | None:
    return _insert(
        "insert_balance_snapshot",
        "INSERT INTO balance_snapshots (run_id, ts, mode, tmn_balance, assets_json, "
        "total_value_tmn) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, _utcnow_iso(), mode, tmn_balance, _json(assets), total_value_tmn),
    )


def log_event(level: str, kind: str, message: str, data: Any = None,
              run_id: int | None = None) -> int | None:
    """Append an event row. run_id=None is allowed (pre-run events)."""
    return _insert(
        "log_event",
        "INSERT INTO events (run_id, ts, level, kind, message, data_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, _utcnow_iso(), level, kind, message, _json(data)),
    )


# ---------------------------------------------------------------------------
# Updates (return True on success, False on failure; never raise)
# ---------------------------------------------------------------------------


def update_decision(decision_id: int, **fields: Any) -> bool:
    """Update mutable decision columns; unknown fields fail (False + stderr)."""
    return _apply_update("update_decision", "decisions", _DECISION_FIELDS, decision_id, fields)


def update_order(order_id: int, **fields: Any) -> bool:
    """Update mutable order columns; updated_at is refreshed automatically."""
    return _apply_update("update_order", "orders", _ORDER_FIELDS, order_id, fields,
                         touch="updated_at")


def update_position(position_id: int, **fields: Any) -> bool:
    """Update mutable position columns; unknown fields fail (False + stderr)."""
    return _apply_update("update_position", "positions", _POSITION_FIELDS, position_id, fields)


def close_position(position_id: int, close_price: float, close_reason: str,
                   pnl_tmn: float, fees_tmn: float,
                   exit_order_id: int | None = None) -> bool:
    """Mark a position CLOSED (status, closed_at, close_* and pnl fields)."""
    return _update(
        "close_position",
        "UPDATE positions SET status = 'CLOSED', closed_at = ?, close_price = ?, "
        "close_reason = ?, pnl_tmn = ?, fees_tmn = ?, exit_order_id = ? WHERE id = ?",
        (_utcnow_iso(), close_price, close_reason, pnl_tmn, fees_tmn, exit_order_id, position_id),
    )


# ---------------------------------------------------------------------------
# Reads (raise when the store is not initialized; other errors propagate)
# ---------------------------------------------------------------------------


def get_open_positions(dry_run: bool) -> list[dict]:
    conn = _require_conn()
    with _lock:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' AND dry_run = ? ORDER BY id",
            (int(bool(dry_run)),),
        ).fetchall()
    return [dict(r) for r in rows]


def get_position(position_id: int) -> dict | None:
    conn = _require_conn()
    with _lock:
        row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    return dict(row) if row is not None else None


def get_order_by_client_id(client_id: str) -> dict | None:
    conn = _require_conn()
    with _lock:
        row = conn.execute("SELECT * FROM orders WHERE client_id = ?", (client_id,)).fetchone()
    return dict(row) if row is not None else None


def get_orders_by_status(statuses: list, dry_run: bool) -> list[dict]:
    """Return orders whose status is in `statuses` and dry_run matches."""
    conn = _require_conn()
    placeholders = ", ".join("?" for _ in statuses)
    with _lock:
        rows = conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) AND dry_run = ? ORDER BY id",
            (*statuses, int(bool(dry_run))),
        ).fetchall()
    return [dict(r) for r in rows]


def realized_pnl_since(since_ts: str, dry_run: bool) -> float:
    """Sum of pnl_tmn over CLOSED positions with closed_at >= since_ts."""
    conn = _require_conn()
    with _lock:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_tmn), 0) AS total FROM positions "
            "WHERE status = 'CLOSED' AND dry_run = ? AND closed_at >= ?",
            (int(bool(dry_run)), since_ts),
        ).fetchone()
    return float(row["total"])


def count_trades_since(since_ts: str, dry_run: bool) -> int:
    """Number of CLOSED positions with closed_at >= since_ts (per mode)."""
    conn = _require_conn()
    with _lock:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM positions "
            "WHERE status = 'CLOSED' AND dry_run = ? AND closed_at >= ?",
            (int(bool(dry_run)), since_ts),
        ).fetchone()
    return int(row["n"])


def get_last_trade_ts(symbol: str, dry_run: bool) -> str | None:
    """Latest closed_at among CLOSED positions of this symbol and mode; None if none."""
    conn = _require_conn()
    with _lock:
        row = conn.execute(
            "SELECT MAX(closed_at) AS last_ts FROM positions "
            "WHERE status = 'CLOSED' AND symbol = ? AND dry_run = ?",
            (symbol, int(bool(dry_run))),
        ).fetchone()
    return row["last_ts"]


def get_last_closed_at(dry_run: bool) -> str | None:
    """Latest closed_at among CLOSED positions of this mode (any symbol); None if none."""
    conn = _require_conn()
    with _lock:
        row = conn.execute(
            "SELECT MAX(closed_at) AS last_ts FROM positions "
            "WHERE status = 'CLOSED' AND dry_run = ?",
            (int(bool(dry_run)),),
        ).fetchone()
    return row["last_ts"]


def get_recent_decisions(symbol: str, limit: int, mode: str) -> list[dict]:
    """Most-recent-first decision rows for one symbol of one mode ('paper'/'live').

    Mode is resolved through the runs table (decisions carry only run_id), so
    paper and live history never mix.
    """
    conn = _require_conn()
    with _lock:
        rows = conn.execute(
            "SELECT d.* FROM decisions d JOIN runs r ON d.run_id = r.id "
            "WHERE d.symbol = ? AND r.mode = ? "
            "ORDER BY d.id DESC LIMIT ?",
            (symbol, mode, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Paper account (singleton row id = 1)
# ---------------------------------------------------------------------------


def _ensure_paper_account_locked(conn: sqlite3.Connection) -> None:
    """Create the paper_account row on first use. Caller holds _lock."""
    row = conn.execute("SELECT id FROM paper_account WHERE id = 1").fetchone()
    if row is None:
        capital = float(config.CONFIG.trading.paper_initial_capital_tmn)
        now = _utcnow_iso()
        conn.execute(
            "INSERT INTO paper_account (id, tmn_balance, assets_json, initial_capital_tmn, "
            "created_at, updated_at) VALUES (1, ?, '{}', ?, ?, ?)",
            (capital, capital, now, now),
        )
        conn.commit()


def get_paper_account() -> dict | None:
    """Return the paper account (assets parsed back into a dict).

    Creates the row on first use with config.trading.paper_initial_capital_tmn.
    Never raises (it performs a write): returns None on failure.
    """
    try:
        conn = _require_conn()
        with _lock:
            _ensure_paper_account_locked(conn)
            row = conn.execute("SELECT * FROM paper_account WHERE id = 1").fetchone()
            result = dict(row)
        result["assets"] = _parse_json_text(result.pop("assets_json"))
        return result
    except Exception as exc:
        print(f"[store.db] get_paper_account failed: {exc}", file=sys.stderr)
        return None


def set_paper_account(tmn_balance: float, assets: dict) -> bool:
    """Update the paper account balance and assets; True on success."""
    try:
        conn = _require_conn()
        with _lock:
            _ensure_paper_account_locked(conn)
            conn.execute(
                "UPDATE paper_account SET tmn_balance = ?, assets_json = ?, updated_at = ? "
                "WHERE id = 1",
                (tmn_balance, _json(assets), _utcnow_iso()),
            )
            conn.commit()
        return True
    except Exception as exc:
        print(f"[store.db] set_paper_account failed: {exc}", file=sys.stderr)
        return False


def reset_paper_account(initial_capital_tmn: float) -> bool:
    """Reset the paper account to the given capital with empty assets."""
    try:
        conn = _require_conn()
        with _lock:
            _ensure_paper_account_locked(conn)
            conn.execute(
                "UPDATE paper_account SET tmn_balance = ?, assets_json = '{}', "
                "initial_capital_tmn = ?, updated_at = ? WHERE id = 1",
                (float(initial_capital_tmn), float(initial_capital_tmn), _utcnow_iso()),
            )
            conn.commit()
        return True
    except Exception as exc:
        print(f"[store.db] reset_paper_account failed: {exc}", file=sys.stderr)
        return False
