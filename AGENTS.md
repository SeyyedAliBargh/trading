# Wallex Trading Agent

Fully autonomous SPOT trading agent for the Wallex AI trading challenge. No human approval.
Return is measured in TOMAN, so the agent trades only to realize profit in toman.
LLMs make the trading decisions. Code builds the inputs, enforces risk, and executes.

## Sources of truth
- API facts: `docs/wallex_api.md` and `docs/samples/`. NEVER guess endpoints, fields or enums.
  If something is unknown, write a probe script or ask the owner. Do not invent.
- Working base URL: `https://api.wallex.ir` (api.wallex.com does NOT resolve). Header: `x-api-key`.
  Always send the key, even on market-data endpoints.
- LLM API: BoxAPI, OpenAI-compatible, `https://ai.boxapi.ir/ai/v1/chat/completions`, `Authorization: Bearer`.
  The client appends /chat/completions to LLM_BASE_URL (https://ai.boxapi.ir/ai/v1).

## Two modes, one switch
- `DRY_RUN=True` (paper mode): real market data, simulated trading on a virtual toman wallet.
  No order is ever sent to Wallex. Balances come from the paper account, not from Wallex.
- `DRY_RUN=False` (live mode): real orders, real balances from Wallex.
- Switching mode = changing only `DRY_RUN` in `.env`. Nothing else in the code path changes.
- At startup print a loud banner with the mode. Store the mode in `runs`, and `dry_run` on every
  order and position row. Paper and live data are never mixed; startup sync only touches the current mode.
- Paper account: starts with `paper_initial_capital_tmn`, persisted in SQLite so restarts keep it,
  resettable by `scripts/reset_paper.py`. Simulated fills use the REAL order book:
  BUY LIMIT fills only if the limit price is at or above the best ask (walk the ask depth, real fee rate);
  MARKET SELL walks the bid depth. Fees and spread are always charged. Never fill at a better price than the book shows.
- Paper mode still calls the read-only authenticated GET /v1/account/fee to get the real fee rate; if that call fails it uses trading.paper_fallback_fee_rate and logs an event.
- Report honestly in docs that paper results are optimistic (no queue position, no latency).

## Hard rules (never violate)
1. No ENTRY order is sent without passing `risk/guard.py`.
2. Any error, timeout, invalid JSON or rate limit on the DECISION path means HOLD, never an entry.
3. EXITS are exempt from entry-only vetoes (min spacing, daily loss cap, trade-count cap, max positions).
   Stop-loss and take-profit closes must NEVER be blocked by guard or by any model vote.
4. Stop-loss / take-profit: the guardian thread checks every open position every few seconds
   against the executable price (best bid for a long position). When hit, close immediately with a
   MARKET sell. If the close fails: retry at once, log an ERROR event, keep retrying until closed.
   Server-side stop orders (`stop_Price`) are NOT used for now (`USE_SERVER_STOP=False`); their semantics are undocumented.
5. Spot only: BUY to enter, SELL to exit. No shorts, no leverage.
6. Only symbols in `config.SYMBOLS` may be traded. One open position per symbol.
7. The agent runs until stopped (Ctrl+C or a future UI button). There is NO end-time logic in code.
   On shutdown: stop threads cleanly, flush logs, list open positions in `events` and print a warning.
   If `CLOSE_POSITIONS_ON_SHUTDOWN=True`, close all positions with MARKET sells before exiting.
8. State (positions, orders) lives in SQLite. On startup, sync with the exchange (live) or the paper account (paper).
9. Logging must never crash the bot. A DB write failure goes to stderr only.
10. Secrets only in `.env` (git-ignored). `.env` is never committed. Never print or store API keys.
11. LLMs decide; the guard only vetoes. Do not replace model decisions with if/else trading logic.

## Entries and exits
- Entry: LIMIT BUY at the best ask (plus `entry_limit_ticks_above` ticks, default 0), wait
  `entry_fill_timeout_sec`; if not filled, cancel and treat as HOLD.
- Exit: MARKET SELL (stop-loss, take-profit, or model SELL vote).
- Models are also called while a position is open and may vote SELL to exit
  (`MODELS_VOTE_ON_EXIT=True`).

## Stop-loss / take-profit modes
- `SLTP_MANUAL` (bool in config):
  - True: fixed `manual_stop_pct` and `manual_target_pct` from config.
  - False: median of the agreeing models' `stop_pct` and `target_pct`.
- In BOTH modes the values are clamped to `[stop_pct_min, stop_pct_max]` and `[target_pct_min, target_pct_max]`.
  All percentages are relative to the position value (entry price), so the loss in toman equals
  position_size_tmn * stop_pct.

## Config
- Single `config.py`: frozen dataclasses with defaults (symbols, model names, risk numbers,
  intervals, flags) and secrets loaded from `.env` via python-dotenv.
- No module outside config.py may hardcode a symbol or assume exactly 4 symbols; loops, per-symbol locks, trigger state and the guard must all iterate over CONFIG.SYMBOLS.
- A config snapshot WITHOUT secrets is saved in the `runs` table at every start.
- `.env.example` lists all variable names with empty values.

## Concurrency
- Plain threads and `requests` (sync). No asyncio.
- Main loop thread: every `DECISION_INTERVAL_SEC`, for each symbol, build input, call the 3 models
  IN PARALLEL (ThreadPoolExecutor, one request per model, per-model timeout), vote, guard, execute.
- Guardian thread: positions, SL/TP, order-state sync.
- One global `threading.Lock` around order placement and position/state changes.

## Decision flow
build input (code) -> N models in parallel -> parse JSON (invalid = that model votes HOLD)
-> majority of same direction else HOLD -> guard (entries) -> executor.
Model JSON: `action` (BUY|SELL|HOLD), `confidence` (0..1), `stop_pct`, `target_pct`, `reason`.
Models may only cite numbers present in the input.

## Event trigger
- The guardian thread already reads prices every GUARDIAN_INTERVAL_SEC.
- For symbols without an open position, if the price moved more than `trigger_move_pct` within
  `trigger_window_sec` and `trigger_cooldown_sec` has passed since the last trigger for that symbol,
  it requests an off-schedule decision cycle for that symbol.
- The cycle is the normal path (build input, parallel models, vote, entry guard, executor);
  the trigger only wakes the models, it never decides or places orders.
- A per-symbol lock must prevent two decision cycles for the same symbol running at the same
  time (scheduled and triggered).
- Every trigger is logged as an event of kind "TRIGGER" with the measured move.

## Database (SQLite, WAL, via `store/db.py` only)
runs, api_calls, market_snapshots, decisions, llm_calls, risk_checks, orders, positions,
balance_snapshots, events, paper_account. Every row has `run_id` and `ts`.
Records link by ids: decision -> order -> position. `decisions.input_json` stores the exact model input.
Every table the future UI needs must be readable without touching the bot (the UI is a separate process).

## Style
- Python 3.11+, type hints, no heavy frameworks, no ORM, no dashboard/UI yet.
- One module = one responsibility. Each module has pytest tests. Tests pass before each commit.
- Commit after every step. The delivered code must equal the code that traded.

## Layout
config.py main.py exchange/ data/ ai/ risk/ execution/ store/ backtest/ scripts/ docs/ tests/ ; runtime/ holds the SQLite database (git-ignored).
The executor package is `execution/`, not `exec/`: "exec" is a Python keyword and cannot be imported with a normal import statement.