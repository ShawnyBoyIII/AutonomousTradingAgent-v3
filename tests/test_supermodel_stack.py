from __future__ import annotations

from types import SimpleNamespace

from trading_bot.cli.app import _format_scan_summary
from trading_bot.config.loader import load_settings
from trading_bot.runtime.orchestrator import run_scan
from trading_bot.runtime.orchestrator import _format_scan_details
from trading_bot.strategy.supermodel import build_stacked_signal


def test_supermodel_supports_when_strategy_v3_and_rl_agree() -> None:
    signal = SimpleNamespace(confidence=0.82)
    details = {"v3_total_score": 9.5, "rl_action": 1, "rl_confidence": 0.78}

    stacked = build_stacked_signal("AAPL", signal, details)

    assert stacked.decision == "support"
    assert stacked.score >= 0.72
    assert stacked.to_details()["supermodel_layers"] == (
        "setup:support:0.82,v3:support:0.79,rl:support:0.78"
    )


def test_supermodel_blocks_when_rl_says_sell() -> None:
    signal = SimpleNamespace(confidence=0.9)
    details = {"v3_total_score": 10.0, "rl_action": 2, "rl_confidence": 0.9}

    stacked = build_stacked_signal("AAPL", signal, details)

    assert stacked.decision == "block"
    assert any(layer.name == "rl" and layer.verdict == "block" for layer in stacked.layers)


def test_supermodel_reports_no_signal_without_local_setup() -> None:
    stacked = build_stacked_signal("AAPL", None, {})

    assert stacked.decision == "no_signal"
    assert stacked.to_details()["supermodel_score"] == 0.0


def test_supermodel_scan_details_and_summary_are_visible() -> None:
    details = {
        "supermodel_decision": "support",
        "supermodel_score": 0.8,
        "supermodel_layers": "setup:support:0.82,v3:support:0.79",
    }
    summary = {
        "symbols": 1,
        "approved": 1,
        "green": 1,
        "yellow": 0,
        "rejected": 0,
        "no_signal": 0,
        "errors": 0,
        "supermodel_support": 1,
        "supermodel_caution": 0,
        "supermodel_block": 0,
        "supermodel_no_signal": 0,
    }

    assert "supermodel=support:0.8" in _format_scan_details(details)
    assert "supermodel_support=1" in _format_scan_summary(summary)


def test_supermodel_summary_hidden_when_no_signals(monkeypatch, tmp_path) -> None:
    import trading_bot.runtime.orchestrator as orchestrator

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {tmp_path / 'state.db'}\n",
        encoding="utf-8",
    )
    settings = load_settings(config_file)

    monkeypatch.setattr(orchestrator, "_calculate_portfolio_heat", lambda state, settings: 0.0)
    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda symbol, settings: (None, "daily regime not bullish", {"daily_close": 100.0}),
    )

    result = run_scan(["AAPL"], settings, include_details=True)

    assert "supermodel_support" not in result["summary"]
