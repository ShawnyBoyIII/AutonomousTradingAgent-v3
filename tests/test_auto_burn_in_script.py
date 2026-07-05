from pathlib import Path


def test_auto_burn_in_runs_nightly_tune_after_daily_discovery() -> None:
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    assert "run_nightly_tuning()" in script
    assert 'sh ./tradebot-local --config-path "$CONFIG_FILE" tune' in script
    assert 'run_discovery "daily"' in script
    assert 'run_nightly_tuning' in script


def test_auto_burn_in_runs_advisory_learner_periodically_and_on_shutdown() -> None:
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    assert 'advisory-learn' in script
    assert '--daily-report' in script
    assert 'trap ' in script
    assert 'ADVISORY_ENABLED=' in script
