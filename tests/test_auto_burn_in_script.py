from pathlib import Path


def test_auto_burn_in_runs_nightly_tune_after_daily_discovery() -> None:
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")

    assert "run_nightly_tuning()" in script
    assert 'sh ./tradebot-local --config-path "$CONFIG_FILE" tune' in script
    assert 'run_discovery "daily"' in script
    assert 'run_nightly_tuning' in script
