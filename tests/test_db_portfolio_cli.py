from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from trading_bot.cli.app import app


def test_db_portfolio_reads_empty_snapshot_store(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'trading.db'}\n"
        f"  log_dir: {tmp_path / 'logs'}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "db-portfolio", "--limit", "1"],
    )

    assert result.exit_code == 0, result.stdout
    assert "PORTFOLIO SNAPSHOTS" in result.stdout
    assert "OPEN POSITIONS" in result.stdout
