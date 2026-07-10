"""Regression tests for the 2026-07-10 burn-in tuning pass.

Each test pins a single post-tuning value in ``burn-in-config.yaml`` so a
future operator who reverts any of the knobs fails this test instead of
silently resetting the burn-in to the loosest setting.

The tests load the live burn-in config and assert exact field values; if
the YAML drifts back to the loose defaults that produced today's
-$4,423.26 paper loss and 0.65 profit factor, the suite trips.
"""

from __future__ import annotations

from pathlib import Path


def _settings_from_burn_in_config():
    from trading_bot.config.loader import load_settings

    return load_settings(Path("burn-in-config.yaml"))


def test_risk_min_reward_risk_ratio_was_restored_to_one_point_five() -> None:
    """2026-07-10: min_reward_risk_ratio raised from 1.0 to 1.5 after
    today's 4 worst losers all sat in the 1.0–1.5 R/R bucket."""
    settings = _settings_from_burn_in_config()
    assert settings.risk.min_reward_risk_ratio == 1.5, (
        "Tuning set this to 1.5 to filter out the 1.0–1.5 setups that "
        "produced today's NFLX/LDI/TEM/SOFI stops. Reverting to 1.0 "
        "re-enables the 10:00-hour carnage."
    )


def test_risk_max_daily_orders_was_tightened_to_twenty() -> None:
    """2026-07-10: max_daily_orders reduced from 60 to 20 after the day
    produced 118 fills (≈2/min), the primary driver of the 0.65 PF."""
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_daily_orders == 20, (
        "Tuning set this to 20 to force the burner to wait for confluence. "
        "Raising it back to 60 re-enables the churn that hid today's edge."
    )


def test_risk_max_ticker_allocation_pct_was_tightened_to_ten_percent() -> None:
    """2026-07-10: max_ticker_allocation_pct reduced from 0.25 to 0.10
    after a single NFLX mean-reversion entry took $96,209 (≈12.6% of
    the post-deposit baseline) and produced the day's largest loss."""
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_ticker_allocation_pct == 0.10, (
        "Tuning set this to 0.10 so the NFLX-sized positions that produced "
        "today's $7,448 in top-5 losses cannot repeat. Reverting to 0.25 "
        "re-enables concentrated loss-of-the-day risk."
    )


def test_risk_max_risk_per_trade_pct_was_halved() -> None:
    """2026-07-10: max_risk_per_trade_pct halved from 0.01 to 0.005
    to dampen single-trade asymmetry. Today's worst trades were twice
    as large as the winners (gross loss 1.53x gross win)."""
    settings = _settings_from_burn_in_config()
    assert settings.risk.max_risk_per_trade_pct == 0.005, (
        "Tuning set this to 0.005 to address the winner/loser size "
        "asymmetry that drove the 0.65 profit factor."
    )


def test_counter_thesis_enabled_after_today() -> None:
    """2026-07-10: counter_thesis.enabled flipped from False to True
    so over-extension and overbought checks guard v3-mean_reversion
    entries that produced the day's worst losers."""
    settings = _settings_from_burn_in_config()
    assert settings.counter_thesis.enabled is True, (
        "Tuning enabled counter_thesis to gate today's repeated over-"
        "extended mean-reversion entries. Disabling it reopens the "
        "guardrail that produced -$3,007 on v3-mean_reversion alone."
    )


def test_strategy_tracker_full_allocation_rate_pinned_low() -> None:
    """2026-07-10: state/tuning_overrides.yaml keeps
    strategy_tracker.full_allocation_rate at 0.20 so the tracker never
    hands full allocation to a strategy with PF<1 even when the
    momentary allocation rate test passes."""
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
