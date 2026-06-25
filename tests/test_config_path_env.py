"""Tests for CONFIG_PATH environment variable fallback.

Priority order:
  1. --config-path flag (highest)
  2. CONFIG_PATH env var
  3. config.yaml default (lowest)
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_bot.cli.app import app


def _make_config(tmp_path: Path, name: str = "config.yaml") -> Path:
    """Create a minimal config file in tmp_path."""
    config_file = tmp_path / name
    config_file.write_text(
        "app:\n"
        "  state_db_path: state/trading_bot.db\n"
        "  log_dir: logs\n"
        "  scan_results_path: state/scan_results.json\n"
        "  portfolio_summary_path: state/portfolio_summary.json\n"
        "  dashboard_summary_path: state/dashboard_summary.json\n"
        "  backtest_summary_path: state/backtest_summary.json\n",
        encoding="utf-8",
    )
    return config_file


class TestConfigPathEnvVar:
    def test_env_var_used_when_no_flag(self, tmp_path: Path, monkeypatch) -> None:
        """CONFIG_PATH env var is used when --config-path is not passed."""
        config_file = _make_config(tmp_path, "custom-config.yaml")
        monkeypatch.setenv("CONFIG_PATH", str(config_file))

        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        # doctor output includes provider info, proving config loaded
        assert "provider=" in result.stdout

    def test_flag_overrides_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """--config-path flag takes priority over CONFIG_PATH env var."""
        env_config = _make_config(tmp_path, "env-config.yaml")
        flag_config = _make_config(tmp_path, "flag-config.yaml")
        monkeypatch.setenv("CONFIG_PATH", str(env_config))

        result = CliRunner().invoke(
            app, ["--config-path", str(flag_config), "doctor"]
        )

        assert result.exit_code == 0

    def test_defaults_to_config_yaml_when_no_env_no_flag(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Falls back to config.yaml when neither flag nor env var is set."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CONFIG_PATH", raising=False)
        _make_config(tmp_path, "config.yaml")

        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "provider=" in result.stdout

    def test_env_var_path_must_exist_or_fallback(self, tmp_path: Path, monkeypatch) -> None:
        """When CONFIG_PATH points to a missing file, loader handles gracefully."""
        monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "nonexistent.yaml"))
        # The loader should still work (returns defaults from missing file)
        result = CliRunner().invoke(app, ["doctor"])
        assert result.exit_code == 0
