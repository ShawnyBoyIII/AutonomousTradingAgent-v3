"""Tripwire regression tests for the strict burn-in guard posture.

The 2026-07-28 full paper reset retired the loose July 9 fire-mode
overrides. These tests keep ``burn-in-config.yaml`` aligned with the
strict baseline documented in AGENTS.md.
"""

from __future__ import annotations

from pathlib import Path


def _settings_from_burn_in_config():
    from trading_bot.config.loader import load_settings

    return load_settings(Path("burn-in-config.yaml"))


def test_risk_min_reward_risk_ratio_matches_strict_default() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.min_reward_risk_ratio == 2.0


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
