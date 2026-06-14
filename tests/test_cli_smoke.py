from typer.testing import CliRunner

from trading_bot.cli.app import app


def test_cli_shows_help() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.stdout
    assert "paper-trade" in result.stdout
