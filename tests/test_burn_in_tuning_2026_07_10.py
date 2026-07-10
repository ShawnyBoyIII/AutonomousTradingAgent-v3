"""Tripwire regression tests for the burn-in override posture.

The values in ``burn-in-config.yaml`` here are the loose guardrail
overrides from 2026-07-09 ("loosen the guardrails") that AGENTS.md
documents as ``TEMP FIRE MODE`` overrides the user explicitly wants
preserved ("Leave the temp override in please").  These tests pin those
override values so an accidental revert to the production defaults
fails the suite instead of silently tightening the burn-in back to
defaults the user has actively overridden.

If a future session needs to retune for a specific reason, both this
file and AGENTS.md must be updated together so the two sources stay in
sync.
"""

from __future__ import annotations

from pathlib import Path


def _settings_from_burn_in_config():
    from trading_bot.config.loader import load_settings

    return load_settings(Path("burn-in-config.yaml"))


def test_risk_min_reward_risk_ratio_matches_2026_07_09_override() -> None:
    """min_reward_risk_ratio should be the looser 1.0 override, not the
    production default of 2.0. AGENTS.md marks 1.0 as TEMP FIRE MODE
    ("today's low-vol market produces ATR-based R/R ~1.3")."""
    settings = _settings_from_burn_in_config()
    assert settings.risk.min_reward_risk_ratio == 1.0, (
        "AGENTS.md pins min_reward_risk_ratio=1.0 as a deliberate override "
        "for the 2026-07-09 'loosen the guardrails' directive. Tightening "
        "back to 1.5 (the production default) re-enables a guardrail the "
        "user explicitly asked to leave open."
    )


def test_risk_max_daily_orders_matches_fire_mode_override() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_daily_orders == 60, (
        "FIRE MODE keeps max_daily_orders at 60 so the burner can churn. "
        "Tightening silently caps the burn-in mode the user requested."
    )


def test_risk_max_ticker_allocation_pct_matches_fire_mode_override() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_ticker_allocation_pct == 0.25, (
        "FIRE MODE keeps the 25% per-ticker ceiling so the burner can size "
        "a position larger than the production default. Reducing it "
        "re-enables a guardrail the user explicitly asked to keep open."
    )


def test_risk_max_risk_per_trade_pct_matches_fire_mode_override() -> None:
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_risk_per_trade_pct == 0.01, (
        "FIRE MODE keeps the 1% per-trade risk; the production default of "
        "0.01 is the override target. This test guards the override, not "
        "the production default."
    )


def test_counter_thesis_disabled_in_fire_mode() -> None:
    """counter_thesis stayed disabled in FIRE MODE because over-extension
    filtering was choking entries the user wanted to allow."""
    settings = _settings_from_burn_in_config()
    assert settings.counter_thesis.enabled is False, (
        "FIRE MODE keeps counter_thesis off so the burner can take the "
        "v3-mean_reversion entries the override posture explicitly allows."
    )


def test_strategy_tracker_full_allocation_rate_pinned_low() -> None:
    """state/tuning_overrides.yaml keeps strategy_tracker at 0.20 so
    the tracker never hands full allocation to a strategy with PF<1."""
    import yaml

    path = Path("state/tuning_overrides.yaml")
    text = path.read_text(encoding="utf-8")
    overrides = yaml.safe_load(text) or {}
    tracker = overrides.get("strategy_tracker", {})
    assert tracker.get("full_allocation_rate") == 0.20, (
        "Tuning set strategy_tracker.full_allocation_rate=0.20. "
        "Raising it above the strategy's measured PF implies full-trust "
        "on a sub-1.0 strategy."
    )
