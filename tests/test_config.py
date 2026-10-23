"""Tests for config.py.

These tests NEVER depend on the real .env: every environment variable they
care about is set or deleted with monkeypatch, and load_dotenv is never called
here. build_config() re-reads os.environ on every call, so monkeypatch always
wins over whatever .env provided at import time.
"""

import dataclasses
import json
import warnings
from typing import Any

import pytest

import config


def make_config(**sections: dict[str, Any]) -> config.Config:
    """A Config built from the placeholder-free defaults, with optional per-section overrides.

    Usage: make_config(risk={"stop_pct_min": 0.03}, trading={"SYMBOLS": ["<X>"]})
    """
    base = config.CONFIG
    parts: dict[str, Any] = {
        "exchange": base.exchange,
        "llm": base.llm,
        "trading": base.trading,
        "risk": base.risk,
        "storage": base.storage,
    }
    for section, updates in sections.items():
        parts[section] = dataclasses.replace(parts[section], **updates)
    return config.Config(**parts)


class TestConfigLoads:
    def test_global_config_exists(self):
        assert isinstance(config.CONFIG, config.Config)

    def test_expected_defaults(self):
        # Only env-independent fields are asserted here (secrets and dry_run
        # come from the environment and are covered by their own tests).
        assert config.CONFIG.exchange.WALLEX_BASE_URL == "https://api.wallex.ir"
        assert config.CONFIG.exchange.request_timeout_sec == 10
        assert config.CONFIG.llm.LLM_BASE_URL == "https://ai.boxapi.ir/ai/v1"
        assert config.CONFIG.llm.per_model_timeout_sec == 20
        assert config.CONFIG.llm.MODELS == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.4-mini"]
        assert config.CONFIG.llm.min_agreeing == 2
        assert config.CONFIG.trading.SYMBOLS == ["BTCTMN", "ETHTMN", "SOLTMN", "XRPTMN"]
        assert config.CONFIG.trading.DECISION_INTERVAL_SEC == 900
        assert config.CONFIG.trading.GUARDIAN_INTERVAL_SEC == 10
        assert config.CONFIG.trading.CLOSE_POSITIONS_ON_SHUTDOWN is False
        assert config.CONFIG.trading.USE_NEWS is False
        assert config.CONFIG.trading.USE_SERVER_STOP is False
        assert config.CONFIG.trading.MODELS_VOTE_ON_EXIT is True
        assert config.CONFIG.trading.entry_limit_ticks_above == 0
        assert config.CONFIG.trading.entry_fill_timeout_sec == 10
        assert config.CONFIG.trading.entry_max_slippage_pct == 0.003
        assert config.CONFIG.trading.paper_buy_fee_in_base is True
        assert config.CONFIG.trading.event_trigger_enabled is True
        assert config.CONFIG.trading.trigger_move_pct == 0.015
        assert config.CONFIG.trading.trigger_window_sec == 300
        assert config.CONFIG.trading.trigger_cooldown_sec == 180
        assert config.CONFIG.trading.paper_initial_capital_tmn == 5_000_000
        assert config.CONFIG.trading.paper_fallback_fee_rate == 0.005
        assert config.CONFIG.trading.SLTP_MANUAL is False
        assert config.CONFIG.trading.manual_stop_pct == 0.015
        assert config.CONFIG.trading.manual_target_pct == 0.03
        assert config.CONFIG.trading.LLM_JSON_MODE is False
        assert config.CONFIG.trading.probe_default_symbol == "XRPTMN"
        # STEP P-B execution-layer tunables
        assert config.CONFIG.trading.balance_snapshot_interval_sec == 60
        assert config.CONFIG.trading.order_poll_interval_sec == 1.5
        assert config.CONFIG.trading.close_fill_timeout_sec == 10
        assert config.CONFIG.trading.close_retry_backoff_sec == 2
        assert config.CONFIG.trading.close_retry_backoff_cap_sec == 30
        assert config.CONFIG.trading.close_limit_ticks == 3
        assert config.CONFIG.trading.ratelimit_backoff_start_sec == 2
        assert config.CONFIG.trading.ratelimit_backoff_cap_sec == 60
        assert config.CONFIG.trading.sync_interval_sec == 300
        assert config.CONFIG.trading.heartbeat_interval_sec == 300
        assert config.CONFIG.storage.log_file_path == "runtime/agent.log"
        # per-model LLM timeout: 20s (observed model latency 3.4-7.3s)
        assert config.CONFIG.llm.per_model_timeout_sec == 20
        # observability (UI step)
        assert config.CONFIG.trading.CONSOLE_VERBOSITY == 1
        assert config.CONFIG.trading.guardian_console_interval_sec == 30
        assert config.CONFIG.trading.guardian_watch_log_interval_sec == 30
        assert config.CONFIG.llm.llm_raw_store_max_chars == 200_000
        assert config.CONFIG.llm.llm_reason_max_chars == 400
        assert config.CONFIG.risk.per_trade_fraction == 0.10  # tuned by the owner
        assert config.CONFIG.risk.max_positions == 2
        assert config.CONFIG.risk.max_daily_loss_fraction == 0.03
        assert config.CONFIG.risk.stop_pct_min == 0.01
        assert config.CONFIG.risk.stop_pct_max == 0.02
        assert config.CONFIG.risk.target_pct_min == 0.02
        assert config.CONFIG.risk.target_pct_max == 0.06
        assert config.CONFIG.risk.min_reward_risk == 2.0
        assert config.CONFIG.risk.min_net_edge_pct == 0.002
        assert config.CONFIG.risk.min_seconds_between_trades == 900
        assert config.CONFIG.risk.min_confidence == 0.5  # tuned by the owner
        assert config.CONFIG.risk.min_order_tmn == 100_000
        assert config.CONFIG.risk.initial_capital_tmn == 5_000_000
        assert config.CONFIG.storage.DB_PATH == "runtime/agent.db"

    def test_paper_fallback_fee_rate_is_sane(self):
        # Conservative placeholder taker fee for paper fills: positive, below 0.05.
        fee = config.CONFIG.trading.paper_fallback_fee_rate
        assert isinstance(fee, (int, float))
        assert 0 < fee < 0.05


class TestParseBool:
    def test_true_values(self):
        assert config.parse_bool("true", default=False) is True
        assert config.parse_bool("1", default=False) is True
        assert config.parse_bool("yes", default=False) is True

    def test_false_values(self):
        assert config.parse_bool("false", default=True) is False
        assert config.parse_bool("0", default=True) is False
        assert config.parse_bool("no", default=True) is False

    def test_unset_empty_or_garbage_uses_default(self):
        assert config.parse_bool(None, default=True) is True
        assert config.parse_bool(None, default=False) is False
        assert config.parse_bool("", default=True) is True
        assert config.parse_bool("maybe", default=True) is True
        assert config.parse_bool("maybe", default=False) is False


class TestDryRunEnv:
    def test_true_values(self, monkeypatch):
        for raw in ("true", "1", "yes"):
            monkeypatch.setenv("DRY_RUN", raw)
            assert config.build_config().trading.dry_run is True

    def test_false_values(self, monkeypatch):
        for raw in ("false", "0", "no"):
            monkeypatch.setenv("DRY_RUN", raw)
            assert config.build_config().trading.dry_run is False

    def test_unset_defaults_to_paper(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        assert config.build_config().trading.dry_run is True


class TestValidateValueErrors:
    def test_stop_pct_bounds(self):
        with pytest.raises(ValueError, match="stop_pct_min"):
            config.validate_config(make_config(risk={"stop_pct_min": 0.03}))

    def test_target_pct_bounds(self):
        with pytest.raises(ValueError, match="target_pct_min"):
            config.validate_config(make_config(risk={"target_pct_min": 0.07}))

    def test_manual_stop_outside_bounds(self):
        with pytest.raises(ValueError, match="manual_stop_pct"):
            config.validate_config(make_config(trading={"manual_stop_pct": 0.005}))

    def test_manual_target_outside_bounds(self):
        with pytest.raises(ValueError, match="manual_target_pct"):
            config.validate_config(make_config(trading={"manual_target_pct": 0.10}))

    def test_capital_overcommit(self):
        with pytest.raises(ValueError, match="per_trade_fraction"):
            config.validate_config(make_config(risk={"per_trade_fraction": 0.6}))

    def test_too_few_models_for_majority(self):
        with pytest.raises(ValueError, match="min_agreeing"):
            config.validate_config(make_config(llm={"MODELS": ["only-model"]}))

    def test_trigger_move_pct_must_be_positive(self):
        with pytest.raises(ValueError, match="trigger_move_pct"):
            config.validate_config(make_config(trading={"trigger_move_pct": 0.0}))

    def test_trigger_window_sec_must_be_positive(self):
        with pytest.raises(ValueError, match="trigger_window_sec"):
            config.validate_config(make_config(trading={"trigger_window_sec": 0}))

    def test_trigger_cooldown_sec_must_be_non_negative(self):
        with pytest.raises(ValueError, match="trigger_cooldown_sec"):
            config.validate_config(make_config(trading={"trigger_cooldown_sec": -1}))

    def test_entry_max_slippage_pct_bounds(self):
        with pytest.raises(ValueError, match="entry_max_slippage_pct"):
            config.validate_config(make_config(trading={"entry_max_slippage_pct": 0.0}))
        with pytest.raises(ValueError, match="entry_max_slippage_pct"):
            config.validate_config(make_config(trading={"entry_max_slippage_pct": 0.03}))
        # valid upper bound passes
        config.validate_config(make_config(trading={"entry_max_slippage_pct": 0.02}))

    def test_min_net_edge_pct_bounds(self):
        with pytest.raises(ValueError, match="min_net_edge_pct"):
            config.validate_config(make_config(risk={"min_net_edge_pct": -0.001}))
        with pytest.raises(ValueError, match="min_net_edge_pct"):
            config.validate_config(make_config(risk={"min_net_edge_pct": 0.06}))
        # boundary values pass: 0 and 0.05
        config.validate_config(make_config(risk={"min_net_edge_pct": 0.0}))
        config.validate_config(make_config(risk={"min_net_edge_pct": 0.05}))

    def test_min_order_tmn_must_be_positive(self):
        with pytest.raises(ValueError, match="min_order_tmn"):
            config.validate_config(make_config(risk={"min_order_tmn": 0}))
        with pytest.raises(ValueError, match="min_order_tmn"):
            config.validate_config(make_config(risk={"min_order_tmn": -1.0}))

    def test_step_pb_tunables_must_be_positive(self):
        for key in ("balance_snapshot_interval_sec", "sync_interval_sec",
                    "heartbeat_interval_sec", "order_poll_interval_sec",
                    "close_fill_timeout_sec", "close_retry_backoff_sec",
                    "close_retry_backoff_cap_sec", "ratelimit_backoff_start_sec",
                    "ratelimit_backoff_cap_sec"):
            with pytest.raises(ValueError, match=key):
                config.validate_config(make_config(trading={key: 0}))

    def test_close_limit_ticks_non_negative(self):
        with pytest.raises(ValueError, match="close_limit_ticks"):
            config.validate_config(make_config(trading={"close_limit_ticks": -1}))

    def test_backoff_start_must_not_exceed_cap(self):
        with pytest.raises(ValueError, match="close_retry_backoff_sec"):
            config.validate_config(make_config(
                trading={"close_retry_backoff_sec": 40,
                         "close_retry_backoff_cap_sec": 30}))
        with pytest.raises(ValueError, match="ratelimit_backoff_start_sec"):
            config.validate_config(make_config(
                trading={"ratelimit_backoff_start_sec": 70,
                          "ratelimit_backoff_cap_sec": 60}))

    def test_poll_interval_above_entry_timeout_warns(self):
        with pytest.warns(UserWarning, match="order_poll_interval_sec"):
            config.validate_config(make_config(
                trading={"order_poll_interval_sec": 20.0,
                          "entry_fill_timeout_sec": 10}))

    def test_log_file_path_outside_runtime_warns(self):
        with pytest.warns(UserWarning, match="log_file_path"):
            config.validate_config(make_config(
                storage={"log_file_path": "logs/agent.log"}))

    def test_console_verbosity_must_be_0_1_or_2(self):
        with pytest.raises(ValueError, match="CONSOLE_VERBOSITY"):
            config.validate_config(make_config(trading={"CONSOLE_VERBOSITY": 3}))
        with pytest.raises(ValueError, match="CONSOLE_VERBOSITY"):
            config.validate_config(make_config(trading={"CONSOLE_VERBOSITY": -1}))
        # boundaries pass
        for level in (0, 1, 2):
            config.validate_config(
                make_config(trading={"CONSOLE_VERBOSITY": level}))

    def test_guardian_observability_intervals_must_be_positive(self):
        for key in ("guardian_console_interval_sec",
                    "guardian_watch_log_interval_sec"):
            with pytest.raises(ValueError, match=key):
                config.validate_config(make_config(trading={key: 0}))

    def test_llm_text_limits_must_be_positive(self):
        for key in ("llm_raw_store_max_chars", "llm_reason_max_chars"):
            with pytest.raises(ValueError, match=key):
                config.validate_config(make_config(llm={key: 0}))

    def test_clean_config_passes_silently(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            config.validate_config(make_config())  # no raise, no warning


class TestValidateWarnings:
    def test_target_below_reward_risk_times_stop(self):
        with pytest.warns(UserWarning, match="target_pct_min"):
            config.validate_config(
                make_config(
                    risk={"stop_pct_min": 0.02, "target_pct_min": 0.03},
                    trading={"manual_stop_pct": 0.02},
                )
            )

    def test_placeholder_symbols_warn(self):
        with pytest.warns(UserWarning, match="SYMBOLS"):
            config.validate_config(make_config(trading={"SYMBOLS": ["<SYMBOL_1>"]}))

    def test_placeholder_models_warn(self):
        with pytest.warns(UserWarning, match="MODELS"):
            config.validate_config(make_config(llm={"MODELS": ["<MODEL_1>", "<MODEL_2>"]}))

    def test_probe_default_symbol_not_configured_warns(self):
        with pytest.warns(UserWarning, match="probe_default_symbol"):
            config.validate_config(
                make_config(trading={"SYMBOLS": ["DOGTMN", "CATTMN"]}))

    def test_default_config_produces_no_warnings(self):
        # Defaults now have real symbols and models: no placeholder warning at all.
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            config.validate_config(config.CONFIG)  # never raises, never warns


class TestSecretsNeverLeak:
    def test_missing_secrets_reported_missing(self, monkeypatch):
        monkeypatch.delenv("WALLEX_API_KEY", raising=False)
        monkeypatch.delenv("BOXAPI_API_KEY", raising=False)
        cfg = config.build_config()  # importing/building never fails on missing secrets
        snap = config.config_snapshot(cfg)
        assert snap["exchange"]["api_key"] == "missing"
        assert snap["llm"]["api_key"] == "missing"

    def test_set_secrets_are_masked(self, monkeypatch):
        monkeypatch.setenv("WALLEX_API_KEY", "TEST-SECRET-WALLEX-123")
        monkeypatch.setenv("BOXAPI_API_KEY", "TEST-SECRET-BOXAPI-456")
        cfg = config.build_config()
        snap = config.config_snapshot(cfg)
        assert snap["exchange"]["api_key"] == "set"
        assert snap["llm"]["api_key"] == "set"
        dump = json.dumps(snap, default=str)
        assert "TEST-SECRET-WALLEX-123" not in dump
        assert "TEST-SECRET-BOXAPI-456" not in dump

    def test_banner_has_no_secrets(self, monkeypatch):
        monkeypatch.setenv("WALLEX_API_KEY", "TEST-SECRET-WALLEX-123")
        monkeypatch.setenv("BOXAPI_API_KEY", "TEST-SECRET-BOXAPI-456")
        cfg = config.build_config()
        banner = config.print_mode_banner(cfg)
        assert "TEST-SECRET-WALLEX-123" not in banner
        assert "TEST-SECRET-BOXAPI-456" not in banner

    def test_global_snapshot_uses_markers_regardless_of_env(self):
        snap = config.config_snapshot()
        assert snap["exchange"]["api_key"] in {"set", "missing"}
        assert snap["llm"]["api_key"] in {"set", "missing"}

    def test_snapshot_contains_step4d_fields(self):
        snap = config.config_snapshot()
        # new STEP 4d fields are snapshotted (no secrets among them)
        assert snap["trading"]["LLM_JSON_MODE"] is False
        assert snap["risk"]["min_net_edge_pct"] == 0.002
        assert snap["risk"]["min_order_tmn"] == 100_000
        assert "min_edge_over_cost" not in snap["risk"]  # removed (deprecated)


class TestModeBanner:
    def test_paper_banner_contents(self, monkeypatch):
        monkeypatch.delenv("DRY_RUN", raising=False)
        cfg = config.build_config()
        banner = config.print_mode_banner(cfg)
        assert "PAPER" in banner
        assert "MODEL" in banner  # SLTP_MANUAL=False -> MODEL mode
        for symbol in cfg.trading.SYMBOLS:
            assert symbol in banner
        for model in cfg.llm.MODELS:
            assert model in banner

    def test_live_banner(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        cfg = config.build_config()
        assert "LIVE" in config.print_mode_banner(cfg)

    def test_manual_sltp_banner(self):
        banner = config.print_mode_banner(make_config(trading={"SLTP_MANUAL": True}))
        assert "MANUAL" in banner
