import sys

from typer.testing import CliRunner

from trading_bot.cli.app import app
from trading_bot.main import main


def test_cli_shows_help(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["tradebot", "--help"])

    try:
        main()
    except SystemExit as exc:
        assert exc.code == 0

    captured = capsys.readouterr()
    assert "scan" in captured.out
    assert "paper-trade" in captured.out


def test_scan_command_prints_symbols() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["scan", "--symbols", "AAPL,MSFT"])

    assert result.exit_code == 0
    assert result.stdout.strip().splitlines() == ["AAPL", "MSFT"]


def test_portfolio_command_prints_placeholder_summary() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["portfolio"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "Portfolio summary: paper-only placeholder."
