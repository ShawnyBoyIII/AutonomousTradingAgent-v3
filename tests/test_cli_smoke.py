import sys

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
