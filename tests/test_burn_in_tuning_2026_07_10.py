"""Tripwire regression tests for the burn-in guard posture.

The burn-in keeps strict portfolio and loss controls while using the
operator-approved 1.0 reward/risk floor for this paper experiment. These
tests keep ``burn-in-config.yaml`` aligned with the baseline documented in
AGENTS.md.
"""

from __future__ import annotations

from pathlib import Path


def _settings_from_burn_in_config():
    import yaml

    from trading_bot.config.settings import Settings

    raw = yaml.safe_load(Path("burn-in-config.yaml").read_text(encoding="utf-8"))
    return Settings.model_validate(raw)


def test_risk_min_reward_risk_ratio_matches_burn_in_baseline() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.min_reward_risk_ratio == 1.0


def test_risk_max_daily_orders_matches_strict_default() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_daily_orders == 3


def test_risk_max_ticker_allocation_pct_matches_strict_default() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_ticker_allocation_pct == 0.20


def test_risk_max_risk_per_trade_pct_matches_strict_default() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_risk_per_trade_pct == 0.01


def test_counter_thesis_matches_disabled_default() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.counter_thesis.enabled is False


def test_strategy_tracker_matches_strict_defaults() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.strategy_tracker.window == 20
    assert settings.strategy_tracker.min_win_rate == 0.20
    assert settings.strategy_tracker.full_allocation_rate == 0.50


def test_remaining_risk_guards_match_strict_defaults() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_portfolio_heat_pct == 0.03
    assert settings.risk.min_stop_distance_pct == 3.0
    assert settings.risk.max_shares_per_position == 50
    assert settings.risk.max_consecutive_losses == 5
    assert settings.risk.enable_drawdown_circuit_breaker is True
    assert settings.risk.ticker_reentry_cooldown_minutes == 30


def test_config_tripwire_ignores_runtime_tuning_overrides(monkeypatch) -> None:
    from trading_bot.config import loader

    def inject_runtime_override(settings, _base_dir) -> None:
        settings.strategy_tracker.full_allocation_rate = 0.99

    monkeypatch.setattr(loader, "_load_tuning_overrides", inject_runtime_override)

    settings = _settings_from_burn_in_config()

    assert settings.strategy_tracker.full_allocation_rate == 0.50
