from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

cli_module = importlib.import_module("trading_bot.cli.app")
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


def test_doctor_burn_in_uses_burn_in_config_by_default(monkeypatch, tmp_path):
    """`doctor --burn-in` must probe burn-in-config.yaml's paths when no
    --config-path is given, because the burner always uses burn-in-config
    and writes its scan_results/heartbeat/etc. there. config.yaml's legacy
    `state/scan_results.json` is no longer touched by anyone and would
    produce false-positive FAILs.

    Regression for the 2026-07-28 divergence where the default-config doctor
    reported scan_freshness FAIL while the burner was perfectly healthy.
    """
    from trading_bot.config import loader as loader_module
    from trading_bot.health.types import CheckResult, HealthReport

    captured = {}

    sdir = tmp_path / "state"
    sdir.mkdir()

    def fake_load_settings(path):
        captured["config_path"] = path
        class _A:
            state_db_path = str(sdir / "burn_in.db")
            state_dir = str(sdir)
            scan_results_path = str(sdir / "burn_in/scan.json")
            dashboard_port = 9999
        class _S:
            app = _A()
            market_data = type("M", (), {"cache_db_path": str(sdir / "market_cache.db")})()
        return _S()

    monkeypatch.setattr(loader_module, "load_settings", fake_load_settings)
    # Also patch the binding inside cli.app so the doctor command's
    # `_reload = load_settings as ...` import resolves to our fake.
    monkeypatch.setattr(cli_module, "load_settings", fake_load_settings)

    fake = HealthReport(
        checks=[
            CheckResult(name="pid_alive", status="PASS", detail="ok", observed=None),
            CheckResult(name="scan_freshness", status="PASS", detail="ok", observed=None),
        ],
        generated_at="2026-07-28T13:00:00+00:00",
    )
    from trading_bot.health import runner as runner_module
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    bim = tmp_path / "burn-in-config.yaml"
    bim.write_text("app:\n  state_db_path: state/burn_in.db\n")
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    assert captured.get("config_path") is not None
    assert str(captured["config_path"]).endswith("burn-in-config.yaml"), (
        f"expected burn-in-config.yaml fallback, got {captured['config_path']!r}"
    )


def test_doctor_burn_in_respects_explicit_config_path(monkeypatch, tmp_path):
    """`doctor --burn-in` must respect an explicit `--config-path` instead
    of overriding it with burn-in-config.yaml.

    Regression for the bug introduced in 5319ddb where the doctor
    unconditionally reloaded burn-in-config.yaml whenever --burn-in was
    set, silently discarding the operator's --config-path choice.
    """
    from trading_bot.config import loader as loader_module
    from trading_bot.health.types import CheckResult, HealthReport

    captured = []

    sdir = tmp_path / "state"
    sdir.mkdir()

    def fake_load_settings(path):
        captured.append(str(path))
        class _A:
            state_db_path = str(sdir / "burn_in.db")
            state_dir = str(sdir)
            scan_results_path = str(sdir / "burn_in/scan.json")
            dashboard_port = 9999
        class _S:
            app = _A()
            market_data = type("M", (), {"cache_db_path": str(sdir / "market_cache.db")})()
        return _S()

    monkeypatch.setattr(loader_module, "load_settings", fake_load_settings)
    monkeypatch.setattr(cli_module, "load_settings", fake_load_settings)

    fake = HealthReport(
        checks=[
            CheckResult(name="pid_alive", status="PASS", detail="ok", observed=None),
        ],
        generated_at="2026-07-28T13:00:00+00:00",
    )
    from trading_bot.health import runner as runner_module
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--config-path", "/my/custom-burn.yaml", "doctor", "--burn-in"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    # The explicit --config-path must be honored exactly once — the
    # doctor must NOT silently fall back to burn-in-config.yaml.
    assert captured == ["/my/custom-burn.yaml"], (
        f"explicit --config-path should be honored without fallback; "
        f"got load_settings calls {captured!r}"
    )


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


def test_resolve_dashboard_port_precedence(monkeypatch):
    """resolve_dashboard_port must honor DASHBOARD_PORT env var with
    precedence over the configured settings value, falling back to the
    settings value (then 8000) when the env var is unset or unparseable.
    """
    from trading_bot.cli.app import resolve_dashboard_port

    class FakeApp:
        def __init__(self, port: int, state_dir: str | None = None) -> None:
            self.dashboard_port = port
            self.state_dir = state_dir
            self.state_db_path = "/dev/null"

        def __getattr__(self, name):
            return None

    class FakeSettings:
        def __init__(self, port: int, state_dir: str | None = None) -> None:
            self.app = FakeApp(port, state_dir)

    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    assert resolve_dashboard_port(FakeSettings(8000)) == 8000
    assert resolve_dashboard_port(FakeSettings(9000)) == 9000

    monkeypatch.setenv("DASHBOARD_PORT", "8080")
    assert resolve_dashboard_port(FakeSettings(8000)) == 8080
    assert resolve_dashboard_port(FakeSettings(9000)) == 8080

    monkeypatch.setenv("DASHBOARD_PORT", "  ")
    assert resolve_dashboard_port(FakeSettings(8000)) == 8000

    monkeypatch.setenv("DASHBOARD_PORT", "not-a-port")
    assert resolve_dashboard_port(FakeSettings(8000)) == 8000


def test_resolve_dashboard_port_reads_burn_in_port_file(tmp_path: Path, monkeypatch):
    """When DASHBOARD_PORT env var is unset and settings has the default
    8000, the doctor must discover the burner's actual sidecar port via
    state/burn_in/dashboard.port (written by auto-burn-in.sh when the
    sidecar starts). This lets operators run ``doctor --burn-in`` from
    outside the burner without exporting env vars.
    """
    from trading_bot.cli.app import resolve_dashboard_port

    class FakeApp:
        def __init__(self, state_dir: str) -> None:
            self.dashboard_port = 8000
            self.state_dir = state_dir
            self.state_db_path = str(Path(state_dir) / "burn_in.db")

    class FakeSettings:
        def __init__(self, state_dir: str) -> None:
            self.app = FakeApp(state_dir)

    state_dir = tmp_path / "state"
    burn_in = state_dir / "burn_in"
    burn_in.mkdir(parents=True)
    (burn_in / "dashboard.port").write_text("8080\n")

    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    assert resolve_dashboard_port(FakeSettings(str(state_dir))) == 8080


def test_resolve_dashboard_port_file_overrides_settings(tmp_path: Path, monkeypatch):
    """A dashboard.port file written by the burner takes precedence over
    settings.app.dashboard_port when DASHBOARD_PORT env var is unset.
    This lets the doctor follow the burner's actual sidecar port even
    when the operator's CLI process doesn't inherit the burner's env.
    """
    from trading_bot.cli.app import resolve_dashboard_port

    class FakeApp:
        def __init__(self, state_dir: str) -> None:
            self.dashboard_port = 9000
            self.state_dir = state_dir
            self.state_db_path = str(Path(state_dir) / "burn_in.db")

    class FakeSettings:
        def __init__(self, state_dir: str) -> None:
            self.app = FakeApp(state_dir)

    state_dir = tmp_path / "state"
    burn_in = state_dir / "burn_in"
    burn_in.mkdir(parents=True)
    (burn_in / "dashboard.port").write_text("5555\n")

    monkeypatch.delenv("DASHBOARD_PORT", raising=False)
    # file wins over settings when env var is unset
    assert resolve_dashboard_port(FakeSettings(str(state_dir))) == 5555


def test_resolve_dashboard_port_env_var_beats_file(tmp_path: Path, monkeypatch):
    """DASHBOARD_PORT env var still wins over the burner's port file when
    explicitly set — operators can force a different port without
    touching the file.
    """
    from trading_bot.cli.app import resolve_dashboard_port

    class FakeApp:
        def __init__(self, state_dir: str) -> None:
            self.dashboard_port = 9000
            self.state_dir = state_dir
            self.state_db_path = str(Path(state_dir) / "burn_in.db")

    class FakeSettings:
        def __init__(self, state_dir: str) -> None:
            self.app = FakeApp(state_dir)

    state_dir = tmp_path / "state"
    burn_in = state_dir / "burn_in"
    burn_in.mkdir(parents=True)
    (burn_in / "dashboard.port").write_text("5555\n")

    monkeypatch.setenv("DASHBOARD_PORT", "7777")
    assert resolve_dashboard_port(FakeSettings(str(state_dir))) == 7777


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