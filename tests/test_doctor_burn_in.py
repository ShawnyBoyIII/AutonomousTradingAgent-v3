from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_bot.cli.app import app


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch):
    sd = tmp_path / "state"
    sd.mkdir()
    (sd / "heartbeat.json").write_text(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "cycle": 1,
        "fills": 0,
        "exits": 0,
        "rejects": 0,
    }))
    (sd / "burn_in.pid").write_text(str(os.getpid()))
    monkeypatch.setenv("TRADING_BOT_STATE_DIR", str(sd))
    return sd


def test_doctor_default_unchanged(state_dir: Path):
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_doctor_burn_in_human_output(state_dir: Path, monkeypatch):
    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    fake = HealthReport(
        checks=[
            CheckResult(name="pid_alive", status="PASS", detail="alive", observed=None),
            CheckResult(name="heartbeat_fresh", status="PASS", detail="fresh", observed=None),
            CheckResult(name="dashboard_health", status="WARN", detail="non-200", observed=None),
            CheckResult(name="eod_watchdog", status="PASS", detail="ok", observed=None),
            CheckResult(name="open_positions_consistent", status="PASS", detail="ok", observed=None),
            CheckResult(name="market_data_freshness", status="PASS", detail="ok", observed=None),
        ],
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"])
    assert result.exit_code == 1, result.output
    assert "Summary" in result.output
    assert "[burn-in]" in result.output


def test_doctor_burn_in_json_output(state_dir: Path, monkeypatch):
    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    fake = HealthReport(
        checks=[
            CheckResult(name="pid_alive", status="PASS", detail="alive", observed=None),
            CheckResult(name="heartbeat_fresh", status="PASS", detail="fresh", observed=None),
            CheckResult(name="dashboard_health", status="PASS", detail="ok", observed=None),
            CheckResult(name="eod_watchdog", status="PASS", detail="ok", observed=None),
            CheckResult(name="open_positions_consistent", status="PASS", detail="ok", observed=None),
            CheckResult(name="market_data_freshness", status="PASS", detail="ok", observed=None),
        ],
        generated_at="2026-07-10T09:31:00+00:00",
    )
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["worst_status"] == "PASS"
    assert len(payload["checks"]) == 6


def test_doctor_burn_in_fail_exit_code(state_dir: Path, monkeypatch):
    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    fake = HealthReport(
        checks=[CheckResult(name="pid_alive", status="FAIL", detail="dead", observed=None)],
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"])
    assert result.exit_code == 2


def test_doctor_burn_in_invokes_scan_freshness(state_dir: Path, monkeypatch):
    """The CLI must pass scan_results_path through to the runner so the
    scan_freshness check is included in the burn-in health report.
    """
    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return HealthReport(
            checks=[
                CheckResult(name="pid_alive", status="PASS", detail="ok", observed=None),
                CheckResult(name="scan_freshness", status="PASS", detail="fresh", observed=None),
            ],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    monkeypatch.setattr(runner_module, "run_health_checks", fake_run)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"])
    assert result.exit_code == 0, result.output
    assert "scan_results_path" in captured, "CLI did not forward scan_results_path"
    assert captured["scan_results_path"] is not None
    # scan_freshness appears in the rendered output
    assert "scan_freshness" in result.output