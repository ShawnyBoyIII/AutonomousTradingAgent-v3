from __future__ import annotations

import pytest
from typer.testing import CliRunner

from trading_bot.cli.app import app
from trading_bot.config.settings import Settings


@pytest.mark.parametrize(
    ("configured_v3", "requested", "expected_v3"),
    [
        (True, "v2.5", False),
        (False, "v3", True),
    ],
)
def test_backtest_strategy_option_overrides_config(
    monkeypatch,
    tmp_path,
    configured_v3: bool,
    requested: str,
    expected_v3: bool,
) -> None:
    import trading_bot.backtest.runner as runner

    config = tmp_path / "config.yaml"
    config.write_text(
        "app:\n"
        "  signal_mode: parallel\n"
        "strategy:\n"
        f"  use_v3_signals: {str(configured_v3).lower()}\n",
        encoding="utf-8",
    )
    observed: list[tuple[bool, str]] = []

    def fake_run_backtest(symbols, settings, start=None, end=None):
        observed.append(
            (settings.strategy.use_v3_signals, settings.app.signal_mode)
        )
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "net_pnl": 0.0}

    monkeypatch.setattr(runner, "run_backtest", fake_run_backtest)

    result = CliRunner().invoke(
        app,
        [
            "--config-path",
            str(config),
            "backtest",
            "--symbols",
            "AAPL",
            "--strategy",
            requested,
        ],
    )

    assert result.exit_code == 0
    assert observed == [(expected_v3, "serial")]


def test_strategy_comparison_forces_each_named_strategy_to_serial(monkeypatch) -> None:
    import trading_bot.backtest.runner as runner

    observed: list[tuple[bool, str]] = []

    def fake_run_backtest(symbols, settings, start=None, end=None):
        observed.append((settings.strategy.use_v3_signals, settings.app.signal_mode))
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "net_pnl": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
        }

    monkeypatch.setattr(runner, "run_backtest", fake_run_backtest)
    settings = Settings()
    settings.app.signal_mode = "parallel"

    runner.run_strategy_comparison(
        ["AAPL"],
        settings,
        strategies=["v2.5", "v3"],
    )

    assert observed == [(False, "serial"), (True, "serial")]


def test_backtest_strategy_rejects_unknown_name(tmp_path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "--config-path",
            str(config),
            "backtest",
            "--symbols",
            "AAPL",
            "--strategy",
            "mystery",
        ],
    )

    assert result.exit_code == 2
    assert "v2.5 or v3" in result.stderr
