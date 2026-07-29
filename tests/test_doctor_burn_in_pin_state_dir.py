"""Regression tests for ``doctor --burn-in`` reading from the burner's
pinned snapshot root when ``PIN_DIR`` is set.

Root cause (2026-07-29 class): the launcher exports ``PIN_DIR`` pointing
at the snapshot root ``<pin-parent>/<head_sha>/`` and runs
``auto-burn-in.sh`` from that snapshot, so the live burn-in writes its
``state/burn_in/burn_in.pid``, ``heartbeat.json``, ``eod_watchdog.pid``,
``dashboard.port``, and ``state/burn_in/scan_results.json`` files under
``$PIN_DIR/state/...`` — not the live worktree. ``doctor --burn-in``
run from outside the burner (or against the live worktree, which is
how a manual operator's CLI always runs) currently derives its
``state_dir`` from ``settings.app.state_db_path.parent`` and reads from
the live worktree, where those files do not exist. The doctor then
reports false-positive FAILs even though the pinned burner is healthy.

The boundary: when ``PIN_DIR`` is set AND the snapshot contains both
``scripts/auto-burn-in.sh`` and ``state/burn_in/burn_in.pid`` (i.e. the
burner is actively running against this snapshot), ``doctor --burn-in``
and ``resolve_dashboard_port`` must read from ``$PIN_DIR/state``.

When ``PIN_DIR`` is unset OR the snapshot is stale (the canonical
marker files are missing), the existing live-tree behavior is
preserved unchanged.

The helper lives in ``trading_bot.cli.app._pin_snapshot_state_dir`` and
is the single source of truth for both ``doctor`` and
``resolve_dashboard_port``. Existing doctor/dashboard tests stay
untouched; this file adds additive regression coverage only.
"""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

cli_module = importlib.import_module("trading_bot.cli.app")
from trading_bot.cli.app import app


# --------------------------------------------------------------------- #
# Test helpers: build a minimal "active snapshot" directory in tmp_path.
# --------------------------------------------------------------------- #
def _build_active_snapshot(pin_dir: Path, *, port: str | None = None) -> None:
    """Materialize the canonical files the PIN check requires.

    - ``scripts/auto-burn-in.sh`` marks the directory as a real snapshot.
    - ``state/burn_in/burn_in.pid`` marks the burner as actively running.
    - Optional ``state/burn_in/dashboard.port`` for port-discovery tests.
    """
    pin_dir.mkdir(parents=True, exist_ok=True)
    (pin_dir / "scripts").mkdir(exist_ok=True)
    (pin_dir / "scripts" / "auto-burn-in.sh").write_text("#!/bin/sh\n")
    burn_in = pin_dir / "state" / "burn_in"
    burn_in.mkdir(parents=True, exist_ok=True)
    (burn_in / "burn_in.pid").write_text("99999\n")
    if port is not None:
        (burn_in / "dashboard.port").write_text(f"{port}\n")


def _build_live_state(live_state: Path, *, port: str | None = None, pid: str | None = None) -> None:
    """Materialize a competing live worktree state dir with sentinel values.

    The tests assert the doctor NEVER reads from this directory when
    PIN_DIR is active — sentinel PID and port values let us prove the
    wrong file is not picked up.
    """
    live_state.mkdir(parents=True, exist_ok=True)
    burn_in = live_state / "burn_in"
    burn_in.mkdir(parents=True, exist_ok=True)
    if pid is not None:
        (burn_in / "burn_in.pid").write_text(f"{pid}\n")
    if port is not None:
        (burn_in / "dashboard.port").write_text(f"{port}\n")


@pytest.fixture
def fake_loader(monkeypatch):
    """Patch ``load_settings`` so ``doctor`` does not touch the real config.

    Returns a factory that builds a settings object whose ``state_db_path``
    points at the supplied live_state directory. The test wires
    ``run_health_checks`` separately so it can capture the kwargs.
    """
    from trading_bot.config import loader as loader_module

    captured: list[str | None] = []

    def _factory(live_state: Path):
        sdir = live_state

        def fake_load_settings(path):
            captured.append(str(path))

            class _A:
                state_db_path = str(sdir / "burn_in.db")
                state_dir = str(sdir)
                scan_results_path = str(sdir / "burn_in" / "scan_results.json")
                dashboard_port = 8888

            class _S:
                app = _A()
                market_data = type(
                    "M", (), {"cache_db_path": str(sdir / "market_cache.db")}
                )()

            return _S()

        monkeypatch.setattr(loader_module, "load_settings", fake_load_settings)
        monkeypatch.setattr(cli_module, "load_settings", fake_load_settings)
        return captured

    return _factory


# --------------------------------------------------------------------- #
# Helper unit tests: _pin_snapshot_state_dir
# --------------------------------------------------------------------- #
def test_pin_snapshot_state_dir_active_returns_snapshot_state(monkeypatch, tmp_path):
    """When PIN_DIR points at an active snapshot, the helper returns
    ``$PIN_DIR/state``.
    """
    pin_dir = tmp_path / "pin"
    _build_active_snapshot(pin_dir)

    monkeypatch.setenv("PIN_DIR", str(pin_dir))
    from trading_bot.cli.app import _pin_snapshot_state_dir

    assert _pin_snapshot_state_dir() == pin_dir / "state"


def test_pin_snapshot_state_dir_unset_returns_none(monkeypatch):
    """No PIN_DIR means no active snapshot → None (caller falls back)."""
    monkeypatch.delenv("PIN_DIR", raising=False)
    from trading_bot.cli.app import _pin_snapshot_state_dir

    assert _pin_snapshot_state_dir() is None


def test_pin_snapshot_state_dir_stale_snapshot_returns_none(monkeypatch, tmp_path):
    """PIN_DIR set but the snapshot is stale (no burn_in.pid marker) → None.

    The launcher's ``stop_dashboard`` removes the port file but leaves
    the snapshot directory in place; we must not read from a snapshot
    whose burner has been stopped.
    """
    pin_dir = tmp_path / "pin"
    pin_dir.mkdir()
    (pin_dir / "scripts").mkdir()
    (pin_dir / "scripts" / "auto-burn-in.sh").write_text("#!/bin/sh\n")
    # Deliberately NO state/burn_in/burn_in.pid — snapshot is stale.

    monkeypatch.setenv("PIN_DIR", str(pin_dir))
    from trading_bot.cli.app import _pin_snapshot_state_dir

    assert _pin_snapshot_state_dir() is None


def test_pin_snapshot_state_dir_missing_snapshot_returns_none(monkeypatch, tmp_path):
    """PIN_DIR set but pointing at a directory without the snapshot
    canonical files → None (not a real snapshot).
    """
    pin_dir = tmp_path / "not-a-snapshot"
    pin_dir.mkdir()  # No scripts/, no state/burn_in/burn_in.pid.

    monkeypatch.setenv("PIN_DIR", str(pin_dir))
    from trading_bot.cli.app import _pin_snapshot_state_dir

    assert _pin_snapshot_state_dir() is None


# --------------------------------------------------------------------- #
# (a) doctor --burn-in reads from $PIN_DIR/state when snapshot is active
# --------------------------------------------------------------------- #
def test_doctor_burn_in_reads_pid_heartbeat_scan_from_pin_state(
    monkeypatch, tmp_path, fake_loader
):
    """``doctor --burn-in`` must derive its health state_dir, eod_watchdog
    pid file, and scan_results_path from ``$PIN_DIR/state`` whenever the
    snapshot is active.

    Live worktree sentinel files must NOT be read; the runner's kwargs
    prove the doctor resolved every path under the snapshot.
    """
    pin_dir = tmp_path / "pin"
    _build_active_snapshot(pin_dir)

    live_state = tmp_path / "live"
    _build_live_state(live_state, pid="11111")

    monkeypatch.setenv("PIN_DIR", str(pin_dir))
    fake_loader(live_state)

    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return HealthReport(
            checks=[CheckResult(name="pid_alive", status="PASS", detail="ok", observed=None)],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    monkeypatch.setattr(runner_module, "run_health_checks", fake_run)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    assert Path(captured["state_dir"]) == pin_dir / "state", (
        f"state_dir must come from PIN_DIR; got {captured['state_dir']!r} "
        f"(live would have been {live_state})"
    )
    assert Path(captured["eod_watchdog_pid_file"]) == pin_dir / "state" / "burn_in" / "eod_watchdog.pid"
    assert Path(captured["scan_results_path"]) == pin_dir / "state" / "burn_in" / "scan_results.json", (
        f"scan_results_path must be rebased to PIN_DIR; got {captured['scan_results_path']!r}"
    )


def test_doctor_burn_in_falls_back_to_live_when_pin_dir_unset(
    monkeypatch, tmp_path, fake_loader
):
    """No PIN_DIR → standard live-worktree behavior preserved."""
    monkeypatch.delenv("PIN_DIR", raising=False)

    live_state = tmp_path / "live"
    _build_live_state(live_state, pid="22222")

    fake_loader(live_state)

    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return HealthReport(
            checks=[CheckResult(name="pid_alive", status="PASS", detail="ok", observed=None)],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    monkeypatch.setattr(runner_module, "run_health_checks", fake_run)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    assert Path(captured["state_dir"]) == live_state, (
        f"without PIN_DIR, state_dir must come from settings; got {captured['state_dir']!r}"
    )
    assert Path(captured["eod_watchdog_pid_file"]) == live_state / "burn_in" / "eod_watchdog.pid"
    assert Path(captured["scan_results_path"]) == live_state / "burn_in" / "scan_results.json"


def test_doctor_burn_in_falls_back_to_live_when_pin_snapshot_stale(
    monkeypatch, tmp_path, fake_loader
):
    """PIN_DIR set but snapshot is stale (no burn_in.pid) → live fallback.

    A stale snapshot must not silently hijack the doctor's reads —
    we want the live worktree to be probed instead so a stopped
    burner's state directory does not cause spurious FAILs.
    """
    pin_dir = tmp_path / "pin"
    pin_dir.mkdir()
    (pin_dir / "scripts").mkdir()
    (pin_dir / "scripts" / "auto-burn-in.sh").write_text("#!/bin/sh\n")
    # Deliberately NO state/burn_in/burn_in.pid — stale snapshot.

    live_state = tmp_path / "live"
    _build_live_state(live_state, pid="33333")

    monkeypatch.setenv("PIN_DIR", str(pin_dir))
    fake_loader(live_state)

    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return HealthReport(
            checks=[CheckResult(name="pid_alive", status="PASS", detail="ok", observed=None)],
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    monkeypatch.setattr(runner_module, "run_health_checks", fake_run)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"], catch_exceptions=False)
    assert result.exit_code == 0, result.output

    assert Path(captured["state_dir"]) == live_state
    assert Path(captured["eod_watchdog_pid_file"]) == live_state / "burn_in" / "eod_watchdog.pid"
    assert Path(captured["scan_results_path"]) == live_state / "burn_in" / "scan_results.json"


# --------------------------------------------------------------------- #
# resolve_dashboard_port PIN awareness: $PIN_DIR/state/burn_in/dashboard.port
# --------------------------------------------------------------------- #
def test_resolve_dashboard_port_reads_pin_dir_when_active(
    monkeypatch, tmp_path
):
    """When PIN_DIR points at an active snapshot, the dashboard port
    file must be read from ``$PIN_DIR/state/burn_in/dashboard.port`` —
    NOT the live worktree.

    The snapshot's port (9876) differs from the live sentinel (1111)
    so the test proves the right file is picked up.
    """
    pin_dir = tmp_path / "pin"
    _build_active_snapshot(pin_dir, port="9876")

    live_state = tmp_path / "live"
    _build_live_state(live_state, port="1111")

    monkeypatch.setenv("PIN_DIR", str(pin_dir))
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)

    from trading_bot.cli.app import resolve_dashboard_port

    class FakeApp:
        def __init__(self, state_dir: str) -> None:
            self.dashboard_port = 8000
            self.state_dir = state_dir
            self.state_db_path = str(Path(state_dir) / "burn_in.db")

    class FakeSettings:
        def __init__(self, state_dir: str) -> None:
            self.app = FakeApp(state_dir)

    # Settings point at the live tree; PIN_DIR must override.
    assert resolve_dashboard_port(FakeSettings(str(live_state))) == 9876


def test_resolve_dashboard_port_falls_back_to_live_when_pin_dir_unset(
    monkeypatch, tmp_path
):
    """Without PIN_DIR, existing live-state behavior is preserved."""
    monkeypatch.delenv("PIN_DIR", raising=False)
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)

    live_state = tmp_path / "live"
    _build_live_state(live_state, port="2222")

    from trading_bot.cli.app import resolve_dashboard_port

    class FakeApp:
        def __init__(self, state_dir: str) -> None:
            self.dashboard_port = 8000
            self.state_dir = state_dir
            self.state_db_path = str(Path(state_dir) / "burn_in.db")

    class FakeSettings:
        def __init__(self, state_dir: str) -> None:
            self.app = FakeApp(state_dir)

    assert resolve_dashboard_port(FakeSettings(str(live_state))) == 2222


def test_resolve_dashboard_port_falls_back_when_pin_snapshot_stale(
    monkeypatch, tmp_path
):
    """PIN_DIR set but snapshot is stale → live port file wins."""
    pin_dir = tmp_path / "pin"
    pin_dir.mkdir()
    (pin_dir / "scripts").mkdir()
    (pin_dir / "scripts" / "auto-burn-in.sh").write_text("#!/bin/sh\n")
    # No state/burn_in/burn_in.pid; also no dashboard.port under the stale snapshot.

    live_state = tmp_path / "live"
    _build_live_state(live_state, port="3333")

    monkeypatch.setenv("PIN_DIR", str(pin_dir))
    monkeypatch.delenv("DASHBOARD_PORT", raising=False)

    from trading_bot.cli.app import resolve_dashboard_port

    class FakeApp:
        def __init__(self, state_dir: str) -> None:
            self.dashboard_port = 8000
            self.state_dir = state_dir
            self.state_db_path = str(Path(state_dir) / "burn_in.db")

    class FakeSettings:
        def __init__(self, state_dir: str) -> None:
            self.app = FakeApp(state_dir)

    assert resolve_dashboard_port(FakeSettings(str(live_state))) == 3333