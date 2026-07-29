from types import SimpleNamespace

from typer.testing import CliRunner

from trading_bot.cli.app import app
from trading_bot.runtime import continuous_loop


def test_continuous_cli_forwards_only_supported_arguments(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state.db'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n",
        encoding="utf-8",
    )
    received = {}

    def fake_run_continuous_loop(
        *,
        settings,
        interval_seconds,
        max_cycles,
        build_universe,
        dry_run,
        max_failures,
    ):
        received.update(
            interval_seconds=interval_seconds,
            max_cycles=max_cycles,
            build_universe=build_universe,
            dry_run=dry_run,
            max_failures=max_failures,
        )
        return SimpleNamespace(summary=lambda: {})

    monkeypatch.setattr(continuous_loop, "run_continuous_loop", fake_run_continuous_loop)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_path),
            "continuous",
            "--interval",
            "12",
            "--cycles",
            "3",
            "--no-build-universe",
            "--dry-run",
            "--max-failures",
            "4",
        ],
    )

    assert result.exception is None
    assert received == {
        "interval_seconds": 12,
        "max_cycles": 3,
        "build_universe": False,
        "dry_run": True,
        "max_failures": 4,
    }

    help_result = runner.invoke(app, ["continuous", "--help"])
    assert help_result.exit_code == 0
    assert "--event-system" not in help_result.stdout
