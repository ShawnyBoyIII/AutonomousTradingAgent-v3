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


def test_backtest_compare_uses_configured_rl_model_path(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "custom" / "model.zip"
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_bytes(b"")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_compare(symbols, settings, start=None, end=None, strategies=None, model_path=None):
        captured["strategies"] = strategies
        captured["model_path"] = model_path
        return {
            "results": {
                "v2.5": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 10.0},
                "v3": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 12.0},
                "rl": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0},
            },
            "best_pnl_strategy": "v3",
            "best_winrate_strategy": "v3",
        }

    monkeypatch.setattr("trading_bot.backtest.runner.run_strategy_comparison", fake_compare)

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "backtest", "--symbols", "AAPL", "--compare"],
    )

    assert result.exit_code == 0
    assert captured["strategies"] == ["v2.5", "v3", "rl"]
    assert captured["model_path"] == str(model_path)


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


def test_rl_benchmark_requires_existing_model_path(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "rl:\n"
        "  enabled: true\n"
        "  model_path: missing/model.zip\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-benchmark", "--symbol", "AAPL"],
    )

    assert result.exit_code == 1
    assert "requires an existing model path" in result.stdout


def test_rl_benchmark_runs_compare_with_single_symbol(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "market_data:\n"
        "  provider: yfinance\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
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
                "rl": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0},
            },
            "best_pnl_strategy": "v3",
            "best_winrate_strategy": "v3",
        }

    monkeypatch.setattr("trading_bot.backtest.runner.run_strategy_comparison", fake_compare)

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-benchmark", "--symbol", "AAPL"],
    )

    assert result.exit_code == 0
    assert captured["symbols"] == ["AAPL"]
    assert captured["strategies"] == ["v2.5", "v3", "rl"]
    assert captured["model_path"] == str(model_path)
    assert "RL BENCHMARK" in result.stdout


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
