"""CLI coverage for the `tune-experiment` controller command.

The test suite focuses on the new Typer subcommand and the guard the
`tune` command adds to refuse overwrites when an experiment is active.
Both paths are deterministic: the `ExperimentController` is a network-
free, pure-Python object so we can drive it without monkeypatching
``fetch_bars`` or other I/O.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from trading_bot.cli.app import app


def _write_config(tmp_path: Path, state_dir_name: str = "state") -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "app:\n"
        f"  state_db_path: {state_dir_name}/burn_in.db\n"
        "  log_dir: logs\n"
        "  scan_results_path: state/scan_results.json\n"
        "  portfolio_summary_path: state/portfolio_summary.json\n"
        "  dashboard_summary_path: state/dashboard_summary.json\n"
        "  backtest_summary_path: state/backtest_summary.json\n",
        encoding="utf-8",
    )
    return config


def test_tune_experiment_status_no_active(tmp_path: Path) -> None:
    runner = CliRunner()
    (tmp_path / "state" / "tuning_experiments").mkdir(parents=True)
    config = _write_config(tmp_path)

    result = runner.invoke(app, ["--config-path", str(config), "tune-experiment", "status"])

    assert result.exit_code == 0
    assert "active=false" in result.stdout or "No active experiment" in result.stdout


def test_tune_experiment_status_reports_active_experiment(tmp_path: Path) -> None:
    """An experiment on disk surfaces its id and status through ``status``."""
    runner = CliRunner()
    exp_dir = tmp_path / "state" / "tuning_experiments"
    exp_dir.mkdir(parents=True)
    payload = {
        "experiment_id": "2026-07-15T00:00:00Z__supermodel.counter_veto_weight-0.5-to-1.0",
        "status": "PROPOSED",
        "change": {
            "section": "supermodel",
            "field": "counter_veto_weight",
            "baseline": 0.5,
            "candidate": 1.0,
        },
        "started_at": "2026-07-15T00:00:00+00:00",
        "canary_closed_trades": 0,
        "market_sessions": [],
        "baseline_metrics": None,
        "candidate_metrics": None,
        "shadow_metrics": None,
        "last_error": None,
        "rolled_back_at": None,
    }
    (exp_dir / "current.json").write_text(json.dumps(payload), encoding="utf-8")
    config = _write_config(tmp_path)

    result = runner.invoke(app, ["--config-path", str(config), "tune-experiment", "status"])

    assert result.exit_code == 0
    assert "active=true" in result.stdout
    assert "supermodel.counter_veto_weight" in result.stdout


def test_tune_experiment_status_json_output(tmp_path: Path) -> None:
    """``--json`` returns a machine-readable payload mirroring ``status()``."""
    runner = CliRunner()
    (tmp_path / "state" / "tuning_experiments").mkdir(parents=True)
    config = _write_config(tmp_path)

    result = runner.invoke(
        app,
        ["--config-path", str(config), "tune-experiment", "status", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout.strip())
    assert payload == {"active": False}


def test_tune_command_refuses_when_experiment_active(tmp_path: Path) -> None:
    """``tune`` must exit 2 when an experiment is active.

    The operator must run ``tune-experiment evaluate`` or
    ``tune-experiment rollback`` before ``tune`` can overwrite the
    override file; this is the new guard from Task 7.
    """
    runner = CliRunner()
    exp_dir = tmp_path / "state" / "tuning_experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "current.json").write_text(
        json.dumps(
            {
                "experiment_id": "x",
                "status": "CANARY",
                "change": {
                    "section": "supermodel",
                    "field": "counter_veto_weight",
                    "baseline": 0.5,
                    "candidate": 1.0,
                },
                "started_at": "2026-07-15T00:00:00+00:00",
                "canary_closed_trades": 0,
                "market_sessions": [],
                "baseline_metrics": None,
                "candidate_metrics": None,
                "shadow_metrics": None,
                "last_error": None,
                "rolled_back_at": None,
            }
        ),
        encoding="utf-8",
    )
    config = _write_config(tmp_path)

    result = runner.invoke(app, ["--config-path", str(config), "tune"])

    assert result.exit_code == 2
    assert "Tuning experiment is active" in result.stdout


def test_tune_command_dry_run_unaffected_by_active_experiment(tmp_path: Path) -> None:
    """Dry-run preview must remain available even with an active experiment.

    Operators may want to inspect what ``tune`` *would* propose without
    touching the override file. The guard only applies to the write
    path; ``--dry-run`` must still work.
    """
    runner = CliRunner()
    exp_dir = tmp_path / "state" / "tuning_experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "current.json").write_text(
        json.dumps(
            {
                "experiment_id": "x",
                "status": "CANARY",
                "change": {
                    "section": "supermodel",
                    "field": "counter_veto_weight",
                    "baseline": 0.5,
                    "candidate": 1.0,
                },
                "started_at": "2026-07-15T00:00:00+00:00",
                "canary_closed_trades": 0,
                "market_sessions": [],
                "baseline_metrics": None,
                "candidate_metrics": None,
                "shadow_metrics": None,
                "last_error": None,
                "rolled_back_at": None,
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    log_dir = tmp_path / "logs"
    config.write_text(
        "app:\n"
        f"  log_dir: {log_dir}\n"
        "  state_db_path: state/burn_in.db\n"
        "  scan_results_path: state/scan_results.json\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["--config-path", str(config), "tune", "--dry-run"])

    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    # The guard must not have triggered for the dry-run path.
    assert "Tuning experiment is active" not in result.stdout


def test_tune_experiment_unknown_action_reports_bad_parameter(tmp_path: Path) -> None:
    """Unknown actions surface as ``typer.BadParameter`` (exit code 2)."""
    runner = CliRunner()
    (tmp_path / "state" / "tuning_experiments").mkdir(parents=True)
    config = _write_config(tmp_path)

    result = runner.invoke(
        app,
        ["--config-path", str(config), "tune-experiment", "explode"],
    )

    assert result.exit_code == 2
    combined = (result.stdout or "") + (getattr(result, "stderr", None) or "")
    assert "unknown action" in combined or "explode" in combined