from __future__ import annotations

from argparse import Namespace
import importlib
from pathlib import Path

from typer.testing import CliRunner

from scripts import train_rl
from trading_bot.cli.app import app


def test_backtest_compare_only_adds_rl_when_enabled_and_model_exists(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app_module = importlib.import_module("trading_bot.cli.app")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "rl:\n"
        "  enabled: false\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_compare(symbols, settings, start=None, end=None, strategies=None, model_path=None):
        captured["symbols"] = symbols
        captured["strategies"] = strategies
        captured["model_path"] = model_path
        return {
            "results": {
                "v2.5": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 10.0},
                "v3": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 12.0},
            },
            "best_pnl_strategy": "v3",
            "best_winrate_strategy": "v3",
        }

    monkeypatch.setattr("trading_bot.cli.app.run_strategy_comparison", fake_compare, raising=False)
    monkeypatch.setattr("trading_bot.backtest.runner.run_strategy_comparison", fake_compare)
    monkeypatch.setattr(app_module.Path, "exists", lambda self: True)

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "backtest", "--symbols", "AAPL", "--compare"],
    )

    assert result.exit_code == 0
    assert captured["strategies"] == ["v2.5", "v3"]
    assert captured["model_path"] is None


def test_rl_eval_forwards_agent_and_train_symbols(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_main() -> int:
        import sys

        captured["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr("scripts.train_rl.main", fake_main)

    result = CliRunner().invoke(
        app,
        ["rl-eval", "--symbols", "AAPL,MSFT", "--train-symbols", "AAPL,MSFT", "--agent", "DQN", "--episodes", "3"],
    )

    assert result.exit_code == 0
    assert captured["argv"] == [
        "train_rl.py",
        "--evaluate",
        "--symbols",
        "AAPL,MSFT",
        "--agent",
        "DQN",
        "--eval-episodes",
        "3",
        "--train-symbols",
        "AAPL,MSFT",
    ]


def test_evaluate_agent_rejects_symbol_mismatch(capsys) -> None:
    args = Namespace(
        train_symbols="AAPL,MSFT",
        symbols="AAPL",
        eval_episodes=5,
        output_dir="state/rl_logs",
        start_date=None,
        end_date=None,
        agent="PPO",
    )

    exit_code = train_rl.evaluate_agent(args)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "evaluation must use the same symbol set as training" in captured.out
