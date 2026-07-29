"""TDD regression coverage for config-path routing.

Root cause: ``scripts/start-dashboard.sh`` exports ``CONFIG_PATH``, but
``ui/dashboard/main.py::DashboardState`` calls ``load_settings()`` directly
and ``trading_bot/config/loader.py::load_settings`` ignores the env var.
Also: CLI ``serve --config-path`` loads the explicit config into ``ctx.obj``
but the Uvicorn string import creates fresh module state without receiving
that path.

These tests pin down the required precedence:

    explicit ``load_settings(path)`` / CLI flag  >  CONFIG_PATH env  >  config.yaml

All tests in this file are additive — existing ``test_config_path_env.py``
CLI-level coverage is preserved. The contract here is narrower: it proves
the routing works at every layer (loader, dashboard state, serve launcher).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from trading_bot.cli.app import app
from trading_bot.config.loader import load_settings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(path: Path, *, db_filename: str = "burn_in.db") -> Path:
    """Write a minimal config whose state_db_path is identifiable."""
    path.write_text(
        "app:\n"
        f"  state_db_path: {path.parent / db_filename}\n"
        f"  log_dir: {path.parent / 'logs'}\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Layer 1: load_settings() honors CONFIG_PATH when no explicit path given.
# ---------------------------------------------------------------------------


def test_load_settings_uses_CONFIG_PATH_env_when_no_explicit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_settings()`` (no args) must read ``CONFIG_PATH`` from the env
    so the dashboard's eager ``DashboardState()`` import picks up the same
    config the burn-in launcher selected.
    """
    config_file = _write_config(tmp_path / "burn-in.yaml")
    monkeypatch.setenv("CONFIG_PATH", str(config_file))
    # Defensive: ensure no stray config.yaml is in CWD so the test is unambiguous.
    monkeypatch.chdir(tmp_path)

    settings = load_settings()

    expected_db = (tmp_path / "burn_in.db").resolve()
    assert Path(settings.app.state_db_path).resolve() == expected_db


def test_load_settings_explicit_path_overrides_CONFIG_PATH_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``Path`` arg must win over ``CONFIG_PATH`` so caller intent
    is never silently overridden by an ambient env var.
    """
    env_config = _write_config(tmp_path / "env-config.yaml", db_filename="env.db")
    flag_config = _write_config(tmp_path / "flag-config.yaml", db_filename="flag.db")
    monkeypatch.setenv("CONFIG_PATH", str(env_config))

    settings = load_settings(flag_config)

    expected_db = (tmp_path / "flag.db").resolve()
    assert Path(settings.app.state_db_path).resolve() == expected_db


def test_load_settings_falls_back_to_config_yaml_when_no_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With neither arg nor env, the loader falls back to ``config.yaml`` in
    CWD — preserved so existing manual workflows keep working.
    """
    _write_config(tmp_path / "config.yaml", db_filename="default.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CONFIG_PATH", raising=False)

    settings = load_settings()

    assert Path(settings.app.state_db_path).resolve() == (tmp_path / "default.db").resolve()


# ---------------------------------------------------------------------------
# Layer 2: DashboardState() honors CONFIG_PATH via load_settings().
# ---------------------------------------------------------------------------


def test_dashboard_state_loads_settings_from_CONFIG_PATH_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DashboardState()`` constructs at module import time. When the
    launcher exports ``CONFIG_PATH``, the freshly-imported dashboard must
    point at the env-selected DB rather than the default config.yaml.
    """
    config_file = _write_config(tmp_path / "burn-in.yaml")
    monkeypatch.setenv("CONFIG_PATH", str(config_file))
    monkeypatch.chdir(tmp_path)

    from ui.dashboard.main import DashboardState

    fresh = DashboardState()

    expected_db = (tmp_path / "burn_in.db").resolve()
    assert Path(fresh.settings.app.state_db_path).resolve() == expected_db
    assert str(fresh.ledger.db_path).endswith("burn_in.db")


# ---------------------------------------------------------------------------
# Layer 3: CLI `serve --config-path` propagates absolute path before uvicorn.
# ---------------------------------------------------------------------------


def test_serve_command_exports_absolute_config_path_before_uvicorn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve --config-path`` must export the resolved absolute path via
    ``CONFIG_PATH`` so the Uvicorn string-imported dashboard subprocess
    reads the same config the CLI just loaded. Without this, uvicorn's
    fresh ``import ui.dashboard.main`` re-runs ``DashboardState()``
    against config.yaml regardless of --config-path.
    """
    config_file = _write_config(tmp_path / "burn-in.yaml")

    captured: dict[str, object] = {}

    def fake_uvicorn_run(target: str, **kwargs: object) -> None:
        # Snapshot the env at the moment uvicorn would have launched the
        # import. This is the moment ``DashboardState()`` would resolve
        # its settings, so the env must already be set.
        captured["target"] = target
        captured["config_path_env"] = os.environ.get("CONFIG_PATH")

    fake_uvicorn = SimpleNamespace(run=fake_uvicorn_run)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    # Defensive: clear CONFIG_PATH so the test proves the CLI set it.
    monkeypatch.delenv("CONFIG_PATH", raising=False)

    result = CliRunner().invoke(
        app, ["--config-path", str(config_file), "serve"]
    )

    assert result.exit_code == 0, result.stdout
    assert captured["target"] == "ui.dashboard.main:app"
    assert captured["config_path_env"] == str(config_file.resolve())


def test_serve_command_does_not_silently_change_explicit_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: --config-path flag must still override an ambient
    CONFIG_PATH. The serve launcher should propagate the flag's resolved
    path, not the env var.
    """
    env_config = _write_config(tmp_path / "env-config.yaml", db_filename="env.db")
    flag_config = _write_config(tmp_path / "flag-config.yaml", db_filename="flag.db")
    monkeypatch.setenv("CONFIG_PATH", str(env_config))

    captured: dict[str, object] = {}

    def fake_uvicorn_run(target: str, **kwargs: object) -> None:
        captured["config_path_env"] = os.environ.get("CONFIG_PATH")

    fake_uvicorn = SimpleNamespace(run=fake_uvicorn_run)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    result = CliRunner().invoke(
        app, ["--config-path", str(flag_config), "serve"]
    )

    assert result.exit_code == 0, result.stdout
    assert captured["config_path_env"] == str(flag_config.resolve())