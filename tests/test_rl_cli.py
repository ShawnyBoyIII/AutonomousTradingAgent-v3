from __future__ import annotations

from argparse import Namespace
import importlib
from pathlib import Path

from typer.testing import CliRunner

from scripts import train_rl
from trading_bot.cli.app import _format_paper_confidence_gate, app


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


def test_train_rl_evaluate_uses_loaded_agent(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "PPO_final.zip"
    model_path.write_bytes(b"model")
    captured: dict[str, object] = {}

    class FakeLoadedAgent:
        def __init__(self):
            self.config = Namespace(env_config=None, training=None)
            self._trainer = object()

        def evaluate(self, n_episodes: int):
            captured["episodes"] = n_episodes
            captured["symbols"] = self.config.env_config.symbols
            captured["trainer_reset"] = self._trainer is None
            return {
                "mean_reward": 1.0,
                "std_reward": 0.0,
                "mean_final_equity": 101_000.0,
                "min_final_equity": 101_000.0,
                "max_final_equity": 101_000.0,
            }

    def fake_load(cls_or_path, path=None):
        path = cls_or_path if path is None else path
        captured["model_path"] = Path(path)
        return FakeLoadedAgent()

    monkeypatch.setattr(train_rl, "fetch_training_data", lambda *args, **kwargs: None)
    monkeypatch.setattr("trading_bot.rl.agent.RLAgent.load", fake_load)

    result = train_rl.evaluate_agent(
        Namespace(
            train_symbols="AAPL",
            symbols="AAPL",
            eval_episodes=3,
            start_date=None,
            end_date=None,
            output_dir=str(tmp_path),
            agent="PPO",
        )
    )

    assert result == 0
    assert captured == {
        "model_path": model_path,
        "episodes": 3,
        "symbols": ["AAPL"],
        "trainer_reset": True,
    }


def test_rl_model_info_reports_metadata(tmp_path: Path) -> None:
    model_path = tmp_path / "PPO_final.zip"
    model_path.write_bytes(b"")
    (tmp_path / "PPO_final_meta.json").write_text(
        '{"symbols": ["AAPL"], "agent": "PPO", "seed": 789, "reward_scheme": "risk_adjusted"}',
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "rl-model-info"])

    assert result.exit_code == 0
    assert f"model_path={model_path}" in result.stdout
    assert "symbols=AAPL" in result.stdout
    assert "seed=789" in result.stdout
    assert "reward_scheme=risk_adjusted" in result.stdout
    assert "supported_scan=./tradebot-local scan --symbols AAPL --summary --why" in result.stdout


def test_rl_model_info_suggests_multi_symbol_scan(tmp_path: Path) -> None:
    model_path = tmp_path / "PPO_final.zip"
    model_path.write_bytes(b"")
    (tmp_path / "PPO_final_meta.json").write_text(
        '{"symbols": ["AAPL", "MSFT"], "agent": "PPO"}',
        encoding="utf-8",
    )
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "rl-model-info"])

    assert result.exit_code == 0
    assert "symbols=AAPL,MSFT" in result.stdout
    assert "supported_scan=./tradebot-local scan --symbols AAPL,MSFT --summary --why" in result.stdout


def test_rl_scan_plan_reports_single_symbol_command(tmp_path: Path) -> None:
    model_path = tmp_path / "PPO_final.zip"
    model_path.write_bytes(b"")
    (tmp_path / "PPO_final_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "rl-scan-plan"])

    assert result.exit_code == 0
    assert "status=ready" in result.stdout
    assert "command=./tradebot-local scan --symbols AAPL --summary --why" in result.stdout


def test_rl_scan_plan_reports_multi_symbol_command(tmp_path: Path) -> None:
    model_path = tmp_path / "PPO_final.zip"
    model_path.write_bytes(b"")
    (tmp_path / "PPO_final_meta.json").write_text('{"symbols": ["AAPL", "MSFT"]}', encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["--config-path", str(config_file), "rl-scan-plan"])

    assert result.exit_code == 0
    assert "status=ready" in result.stdout
    assert "command=./tradebot-local scan --symbols AAPL,MSFT --summary --why" in result.stdout


def test_rl_train_forwards_seed(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_main() -> int:
        import sys

        captured["argv"] = list(sys.argv)
        return 0

    monkeypatch.setattr("scripts.train_rl.main", fake_main)

    result = CliRunner().invoke(
        app,
        ["rl-train", "--symbols", "AAPL", "--agent", "PPO", "--timesteps", "7", "--seed", "789"],
    )

    assert result.exit_code == 0
    assert "--seed" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--seed") + 1] == "789"


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


def test_rl_benchmark_runs_compare_with_symbols(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL", "MSFT"]}', encoding="utf-8")
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
        ["--config-path", str(config_file), "rl-benchmark", "--symbols", "AAPL,MSFT"],
    )

    assert result.exit_code == 0
    assert captured["symbols"] == ["AAPL", "MSFT"]
    assert captured["strategies"] == ["v2.5", "v3", "rl"]
    assert captured["model_path"] == str(model_path)
    assert "RL BENCHMARK" in result.stdout
    assert "symbols=AAPL,MSFT" in result.stdout
    assert "expectancy=" in result.stdout


def test_rl_benchmark_reports_market_data_errors(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    def fake_compare(*_args, **_kwargs):
        raise ValueError("All providers failed for AAPL")

    monkeypatch.setattr("trading_bot.backtest.runner.run_strategy_comparison", fake_compare)

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-benchmark", "--symbol", "AAPL"],
    )

    assert result.exit_code == 1
    assert "market data unavailable: All providers failed for AAPL" in result.stdout
    assert "Traceback" not in result.stdout


def test_rl_benchmark_rejects_symbol_outside_model_metadata(tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-benchmark", "--symbol", "MSFT"],
    )

    assert result.exit_code == 1
    assert "RL model not trained for MSFT (trained_symbols=AAPL)" in result.stdout


def test_rl_benchmark_rejects_missing_model_metadata(tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-benchmark", "--symbol", "AAPL"],
    )

    assert result.exit_code == 1
    assert "RL model metadata missing or empty:" in result.stdout


def test_rl_walkforward_requires_existing_model_path(tmp_path: Path) -> None:
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
        ["--config-path", str(config_file), "rl-walkforward", "--symbol", "AAPL"],
    )

    assert result.exit_code == 1
    assert "requires an existing model path" in result.stdout


def test_paper_confidence_gate_uses_configured_starting_cash() -> None:
    result = {
        "windows": [{"results": {"rl": {"net_pnl": 600.0}}}],
        "results": {
            "rl": {
                "trades": 12,
                "net_pnl": 600.0,
                "profit_factor": 1.3,
            }
        },
    }

    output = _format_paper_confidence_gate(result, starting_cash=100_000.0)

    assert "PAPER CONFIDENCE: FAIL" in output
    assert "net_pnl>=5000" in output


def test_rl_walkforward_runs_sequential_windows_for_symbols(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL", "MSFT"]}', encoding="utf-8")
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

    def fake_walkforward(symbols, settings, start=None, end=None, windows=5, model_path=None):
        captured["symbols"] = symbols
        captured["windows"] = windows
        captured["model_path"] = model_path
        return {
            "windows": [
                {
                    "window": 1,
                    "start": "2025-01-01",
                    "end": "2025-02-01",
                    "results": {
                        "v2.5": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 10.0},
                        "v3": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 12.0},
                        "rl": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0},
                    },
                    "best_pnl_strategy": "v3",
                    "best_winrate_strategy": "v3",
                }
            ],
            "results": {
                "v2.5": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 10.0},
                "v3": {"trades": 1, "wins": 1, "losses": 0, "win_rate": 1.0, "net_pnl": 12.0},
                "rl": {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0},
            },
        }

    monkeypatch.setattr("trading_bot.backtest.runner.run_rl_walk_forward", fake_walkforward)

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-walkforward", "--symbols", "AAPL,MSFT", "--windows", "3"],
    )

    assert result.exit_code == 0
    assert captured["symbols"] == ["AAPL", "MSFT"]
    assert captured["windows"] == 3
    assert captured["model_path"] == str(model_path)
    assert "RL FIXED-MODEL WALK-FORWARD" in result.stdout
    assert "symbols=AAPL,MSFT" in result.stdout
    assert "profit_factor=" in result.stdout
    assert "PAPER CONFIDENCE: FAIL" in result.stdout


def test_rl_walkforward_reports_market_data_errors(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    def fake_walkforward(*_args, **_kwargs):
        raise ValueError("All providers failed for AAPL")

    monkeypatch.setattr("trading_bot.backtest.runner.run_rl_walk_forward", fake_walkforward)

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-walkforward", "--symbol", "AAPL"],
    )

    assert result.exit_code == 1
    assert "market data unavailable: All providers failed for AAPL" in result.stdout
    assert "Traceback" not in result.stdout


def test_rl_walkforward_rejects_symbol_outside_model_metadata(tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    (tmp_path / "model_meta.json").write_text('{"symbols": ["AAPL"]}', encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-walkforward", "--symbol", "MSFT"],
    )

    assert result.exit_code == 1
    assert "RL model not trained for MSFT (trained_symbols=AAPL)" in result.stdout


def test_rl_walkforward_rejects_missing_model_metadata(tmp_path: Path) -> None:
    model_path = tmp_path / "model.zip"
    model_path.write_bytes(b"")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state' / 'trading_bot.db'}\n"
        "rl:\n"
        "  enabled: true\n"
        f"  model_path: {model_path}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["--config-path", str(config_file), "rl-walkforward", "--symbol", "AAPL"],
    )

    assert result.exit_code == 1
    assert "RL model metadata missing or empty:" in result.stdout


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
