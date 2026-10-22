"""Configuration for the Wallex SPOT trading agent.

Facts (base URLs, auth header) come from docs/wallex_api.md and AGENTS.md.
Secrets are read from .env via python-dotenv and are NEVER printed, logged or
snapshotted: config_snapshot() replaces every secret with a "set"/"missing"
marker, and print_mode_banner() never includes secrets.

All stop/target percentages are fractions of the position value (entry price),
so the loss in toman equals position_size_tmn * stop_pct (AGENTS.md).
"""

import os
import warnings
from dataclasses import dataclass, field, fields
from typing import Any

from dotenv import load_dotenv

# Environment variable names (see .env.example; secrets live only in .env).
ENV_WALLEX_API_KEY = "WALLEX_API_KEY"
ENV_BOXAPI_API_KEY = "BOXAPI_API_KEY"
ENV_DRY_RUN = "DRY_RUN"

# Names of dataclass fields that hold secrets (masked in config_snapshot()).
_SECRET_FIELDS = frozenset({"api_key"})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_bool(value: str | None, default: bool) -> bool:
    """Parse a boolean from an env-var string.

    True: "1", "true", "yes", "y", "on". False: "0", "false", "no", "n", "off"
    (case-insensitive). Unset, empty or unrecognized values fall back to
    `default` -- this never raises, so importing config is always safe.
    """
    if value is None:
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _is_placeholder(entry: str) -> bool:
    """True if the entry still looks like an unfilled '<...>' placeholder."""
    text = entry.strip()
    return text.startswith("<") and text.endswith(">")


# ---------------------------------------------------------------------------
# Frozen config sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExchangeConfig:
    """Wallex REST API access (facts from docs/wallex_api.md)."""

    # Base URL that actually resolves; api.wallex.com does NOT resolve (DNS error).
    WALLEX_BASE_URL: str = "https://api.wallex.ir"
    # Seconds before any single request to Wallex is aborted.
    request_timeout_sec: float = 10
    # Wallex API key from .env (WALLEX_API_KEY); always sent in the x-api-key header.
    api_key: str | None = None


@dataclass(frozen=True)
class LlmConfig:
    """BoxAPI (OpenAI-compatible) access for the deciding models."""

    # OpenAI-compatible base URL of the LLM API (client appends /chat/completions).
    LLM_BASE_URL: str = "https://ai.boxapi.ir/ai/v1"
    # BoxAPI key from .env (BOXAPI_API_KEY); sent as "Authorization: Bearer <key>".
    api_key: str | None = None
    # Model names that vote on every decision (one request each, in parallel).
    MODELS: list[str] = field(default_factory=lambda: ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4-mini"])
    # Seconds before a single model call is aborted (one request per model).
    per_model_timeout_sec: float = 20
    # How many models must agree on the same direction for a signal to count.
    min_agreeing: int = 2


@dataclass(frozen=True)
class TradingConfig:
    """Trading-loop behaviour and mode."""

    # Provisional; must be intersected with the organizer's allowed-market list and checked by the market probe (spread, depth).
    SYMBOLS: list[str] = field(default_factory=lambda: ["BTCTMN", "ETHTMN", "SOLTMN", "XRPTMN"])
    # Paper mode (True): virtual toman wallet, no real orders. Live (False): real orders.
    dry_run: bool = True
    # Seconds between decision rounds (per symbol).
    DECISION_INTERVAL_SEC: int = 900
    # Seconds between guardian checks of open positions (SL/TP, order-state sync).
    GUARDIAN_INTERVAL_SEC: int = 10
    # If True, close every open position with a MARKET sell on shutdown.
    CLOSE_POSITIONS_ON_SHUTDOWN: bool = False
    # If True, fetch news and include it in the model input.
    USE_NEWS: bool = False
    # If True, place server-side stop orders (stop_Price); off for now, semantics undocumented.
    USE_SERVER_STOP: bool = False
    # If True, models are also called while a position is open and may vote SELL to exit.
    MODELS_VOTE_ON_EXIT: bool = True
    # If True, LLM requests ask for response_format {"type": "json_object"} (OpenAI-compatible).
    # Off by default: not every backend honors it and the parser is strict anyway.
    LLM_JSON_MODE: bool = False
    # Ticks above the best ask for the entry LIMIT BUY price (0 = join the best ask).
    entry_limit_ticks_above: int = 0
    # Seconds to wait for the entry LIMIT BUY to fill before cancelling (then HOLD).
    entry_fill_timeout_sec: int = 10
    # Maximum acceptable entry slippage above the best ask (0.003 = 0.3%); validated 0 < x <= 0.02.
    entry_max_slippage_pct: float = 0.003
    # If True, paper BUY fees are charged in the BASE asset; if False, in TMN. Real fee asset on Wallex is unverified.
    paper_buy_fee_in_base: bool = True
    # The guardian thread may start an off-schedule decision cycle for a symbol that has NO open position.
    event_trigger_enabled: bool = True
    # Price move within the window that fires the trigger (relative, 0.015 = 1.5%).
    trigger_move_pct: float = 0.015
    # Window length for measuring the move.
    trigger_window_sec: int = 300
    # Minimum seconds between two triggers for the same symbol.
    trigger_cooldown_sec: int = 180
    # Preferred symbol for the live order probe (scripts/probe_orders.py);
    # used only if it is also in SYMBOLS, otherwise the probe falls back to
    # the lowest-priced CONFIG.SYMBOLS entry.
    probe_default_symbol: str = "XRPTMN"
    # Starting balance of the paper account, in toman (paper mode only).
    paper_initial_capital_tmn: float = 5_000_000
    # conservative placeholder taker fee used in paper mode only if GET /v1/account/fee fails; replace after the real fee is known
    paper_fallback_fee_rate: float = 0.005
    # True: fixed manual_stop_pct/manual_target_pct; False: median of the agreeing models.
    SLTP_MANUAL: bool = False
    # Fixed stop-loss as a fraction of the position value (SLTP_MANUAL mode only).
    manual_stop_pct: float = 0.015
    # Fixed take-profit as a fraction of the position value (SLTP_MANUAL mode only).
    manual_target_pct: float = 0.03


@dataclass(frozen=True)
class RiskConfig:
    """Hard risk limits enforced by risk/guard.py before any ENTRY (exits are exempt)."""

    # Fraction of current capital allocated to one trade (position size = capital * fraction).
    per_trade_fraction: float = 0.20
    # Maximum number of simultaneously open positions.
    max_positions: int = 2
    # Stop taking entries for the rest of the day after losing this fraction of capital.
    max_daily_loss_fraction: float = 0.03
    # Lower clamp for every stop-loss fraction (of position value), from any source.
    stop_pct_min: float = 0.01
    # Upper clamp for every stop-loss fraction (of position value), from any source.
    stop_pct_max: float = 0.02
    # Lower clamp for every take-profit fraction (of position value), from any source.
    target_pct_min: float = 0.02
    # Upper clamp for every take-profit fraction (of position value), from any source.
    target_pct_max: float = 0.06
    # Minimum acceptable reward/risk ratio (target_pct / stop_pct) for an entry.
    min_reward_risk: float = 2.0
    # Minimum expected net edge of an entry: p*target - (1-p)*stop - round_trip_cost,
    # where p is the mean confidence of the agreeing models. Replaces the deprecated
    # min_edge_over_cost (edge measured in multiples of cost) from STEP 4d on.
    min_net_edge_pct: float = 0.002
    # Minimum seconds between two entries (exit orders are never blocked by this).
    min_seconds_between_trades: int = 900
    # Minimum model confidence (0..1) required for an entry.
    min_confidence: float = 0.6
    # Hard floor for the order size in toman (our own risk floor; the exchange's
    # real spot minimum is still unknown -- probes found none in the markets response).
    min_order_tmn: float = 100_000
    # Capital at the start of a live run, in toman.
    initial_capital_tmn: float = 5_000_000


@dataclass(frozen=True)
class StorageConfig:
    """SQLite state storage."""

    # Path of the SQLite database (WAL mode) in the git-ignored runtime/ directory.
    DB_PATH: str = "runtime/agent.db"


@dataclass(frozen=True)
class Config:
    """Root configuration aggregating all sections."""

    exchange: ExchangeConfig
    llm: LlmConfig
    trading: TradingConfig
    risk: RiskConfig
    storage: StorageConfig


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_config(cfg: Config) -> None:
    """Validate a Config.

    Raises ValueError for hard errors; only warns (warnings.warn, never
    raises) for soft ones. Called whenever a Config is created.
    """
    r = cfg.risk
    t = cfg.trading

    # --- hard errors -------------------------------------------------------
    if r.stop_pct_min > r.stop_pct_max:
        raise ValueError(f"stop_pct_min ({r.stop_pct_min}) > stop_pct_max ({r.stop_pct_max})")
    if r.target_pct_min > r.target_pct_max:
        raise ValueError(f"target_pct_min ({r.target_pct_min}) > target_pct_max ({r.target_pct_max})")
    if not (r.stop_pct_min <= t.manual_stop_pct <= r.stop_pct_max):
        raise ValueError(
            f"manual_stop_pct ({t.manual_stop_pct}) outside "
            f"[stop_pct_min, stop_pct_max] = [{r.stop_pct_min}, {r.stop_pct_max}]"
        )
    if not (r.target_pct_min <= t.manual_target_pct <= r.target_pct_max):
        raise ValueError(
            f"manual_target_pct ({t.manual_target_pct}) outside "
            f"[target_pct_min, target_pct_max] = [{r.target_pct_min}, {r.target_pct_max}]"
        )
    if r.per_trade_fraction * r.max_positions > 1:
        raise ValueError(
            f"per_trade_fraction * max_positions = {r.per_trade_fraction * r.max_positions} > 1: "
            "open positions could commit more capital than the account holds"
        )
    if len(cfg.llm.MODELS) < cfg.llm.min_agreeing:
        raise ValueError(
            f"len(MODELS) ({len(cfg.llm.MODELS)}) < min_agreeing ({cfg.llm.min_agreeing}): "
            "a majority vote would be impossible"
        )
    if t.trigger_move_pct <= 0:
        raise ValueError(f"trigger_move_pct ({t.trigger_move_pct}) must be > 0")
    if t.trigger_window_sec <= 0:
        raise ValueError(f"trigger_window_sec ({t.trigger_window_sec}) must be > 0")
    if t.trigger_cooldown_sec < 0:
        raise ValueError(f"trigger_cooldown_sec ({t.trigger_cooldown_sec}) must be >= 0")
    if not (0 < t.entry_max_slippage_pct <= 0.02):
        raise ValueError(
            f"entry_max_slippage_pct ({t.entry_max_slippage_pct}) must be in (0, 0.02]"
        )
    if t.probe_default_symbol and t.probe_default_symbol not in t.SYMBOLS:
        warnings.warn(
            f"probe_default_symbol ({t.probe_default_symbol}) is not in SYMBOLS; "
            "the order probe will fall back to the lowest-priced configured symbol",
            stacklevel=2,
        )
    if not (0 <= r.min_net_edge_pct <= 0.05):
        raise ValueError(
            f"min_net_edge_pct ({r.min_net_edge_pct}) must be in [0, 0.05]"
        )
    if r.min_order_tmn <= 0:
        raise ValueError(f"min_order_tmn ({r.min_order_tmn}) must be > 0")

    # --- soft warnings (never raise) ----------------------------------------
    if r.target_pct_min < r.min_reward_risk * r.stop_pct_min:
        warnings.warn(
            f"target_pct_min ({r.target_pct_min}) < min_reward_risk * stop_pct_min "
            f"({r.min_reward_risk * r.stop_pct_min}): some clamped targets may not "
            "satisfy the reward/risk check",
            stacklevel=2,
        )
    placeholder_symbols = [s for s in t.SYMBOLS if _is_placeholder(s)]
    if placeholder_symbols:
        warnings.warn(
            f"SYMBOLS still contains placeholders: {placeholder_symbols}",
            stacklevel=2,
        )
    placeholder_models = [m for m in cfg.llm.MODELS if _is_placeholder(m)]
    if placeholder_models:
        warnings.warn(
            f"MODELS still contains placeholders: {placeholder_models}",
            stacklevel=2,
        )


# ---------------------------------------------------------------------------
# Construction, snapshot, banner
# ---------------------------------------------------------------------------


def build_config() -> Config:
    """Build a validated Config from the current environment variables.

    Never fails on a missing secret (it becomes None and is reported as
    "missing" by config_snapshot()). load_dotenv() has already run at import
    time, so tests can steer this purely via monkeypatched env vars.
    """
    wallex_key = os.environ.get(ENV_WALLEX_API_KEY, "")
    boxapi_key = os.environ.get(ENV_BOXAPI_API_KEY, "")
    cfg = Config(
        exchange=ExchangeConfig(api_key=wallex_key.strip() or None),
        llm=LlmConfig(api_key=boxapi_key.strip() or None),
        trading=TradingConfig(
            dry_run=parse_bool(os.environ.get(ENV_DRY_RUN), default=True),
        ),
        risk=RiskConfig(),
        storage=StorageConfig(),
    )
    validate_config(cfg)
    return cfg


def _section_snapshot(section: Any) -> dict[str, Any]:
    """One config section as a dict, with secret fields masked."""
    out: dict[str, Any] = {}
    for f in fields(section):
        value = getattr(section, f.name)
        if f.name in _SECRET_FIELDS:
            value = "set" if value else "missing"
        out[f.name] = value
    return out


def config_snapshot(cfg: Config | None = None) -> dict[str, Any]:
    """The full config as a dict WITHOUT any secret value.

    Secret fields are replaced by the marker "set" or "missing". This is the
    dict that gets saved in the `runs` table at every start (AGENTS.md).
    """
    cfg = cfg if cfg is not None else CONFIG
    return {
        "exchange": _section_snapshot(cfg.exchange),
        "llm": _section_snapshot(cfg.llm),
        "trading": _section_snapshot(cfg.trading),
        "risk": _section_snapshot(cfg.risk),
        "storage": _section_snapshot(cfg.storage),
    }


def print_mode_banner(cfg: Config | None = None) -> str:
    """Loud multi-line startup banner. Returns a string; contains NO secrets."""
    cfg = cfg if cfg is not None else CONFIG
    mode = "PAPER" if cfg.trading.dry_run else "LIVE"
    if cfg.trading.SLTP_MANUAL:
        sltp = (
            f"MANUAL (stop={cfg.trading.manual_stop_pct:.4g}, "
            f"target={cfg.trading.manual_target_pct:.4g} of position value)"
        )
    else:
        sltp = (
            f"MODEL (median of agreeing models, clamped to "
            f"stop [{cfg.risk.stop_pct_min:.4g}, {cfg.risk.stop_pct_max:.4g}], "
            f"target [{cfg.risk.target_pct_min:.4g}, {cfg.risk.target_pct_max:.4g}])"
        )
    lines = [
        "=" * 72,
        "  WALLEX TRADING AGENT",
        "=" * 72,
        f"  MODE: {mode}   (DRY_RUN={'True' if cfg.trading.dry_run else 'False'})",
        f"  Symbols:     {', '.join(cfg.trading.SYMBOLS) or '(none)'}",
        f"  Models:      {', '.join(cfg.llm.MODELS) or '(none)'}",
        f"  SL/TP mode:  {sltp}",
        f"  Wallex API:  {cfg.exchange.WALLEX_BASE_URL} (x-api-key header)",
        f"  LLM API:     {cfg.llm.LLM_BASE_URL}",
        f"  Database:    {cfg.storage.DB_PATH}",
        "=" * 72,
    ]
    return "\n".join(lines)


# Read .env once at import time (never overrides real environment variables),
# then create the validated global CONFIG. Importing config must never fail
# because a secret is missing (missing secrets only show as "missing" markers).
load_dotenv()
CONFIG = build_config()
