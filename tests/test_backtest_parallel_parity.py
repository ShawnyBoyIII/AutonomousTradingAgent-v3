from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from trading_bot.backtest import runner
from trading_bot.config.settings import Settings
from trading_bot.models.signal import TradeSignal
from trading_bot.runtime import orchestrator


def _signal(confidence: float = 0.9) -> TradeSignal:
    return TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=98.0,
        profit_target=104.0,
        risk_reward_ratio=2.0,
        confidence=confidence,
        reasons=["parallel parity"],
        strategy_tag="v3-trend_following",
        timestamp=datetime(2026, 7, 10, 14, 20, tzinfo=timezone.utc),
    )


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    daily = pd.DataFrame({"close": [100.0]})
    intraday = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-07-10 14:00", periods=7, freq="5min", tz="UTC"),
            "open": [100.0] * 7,
            "high": [101.0, 101.0, 101.0, 101.0, 101.0, 102.0, 105.0],
            "low": [99.0] * 7,
            "close": [100.0, 100.0, 100.0, 100.0, 100.0, 101.0, 104.0],
            "volume": [1000] * 7,
            "volume_avg_5": [1000.0] * 7,
        }
    )
    return daily, intraday


def test_causal_backtest_uses_shared_parallel_resolver(monkeypatch) -> None:
    daily, intraday = _frames()
    calls: list[int] = []

    def fake_parallel(*args, **kwargs):
        calls.append(len(kwargs["intraday_frame"]))
        return _signal(), "parallel consensus (2/2)", {
            "signal_mode": "parallel",
            "consensus": "BUY",
            "is_full_size": True,
        }

    monkeypatch.setattr(orchestrator, "_build_parallel_signal_result", fake_parallel)
    monkeypatch.setattr(
        runner,
        "generate_signal",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("serial path used")),
    )
    settings = Settings()
    settings.app.signal_mode = "parallel"

    result = runner._run_symbol_backtest("AAPL", daily, intraday, settings)

    assert calls == [5]
    assert result["trades"] == 1
    assert result["wins"] == 1


def test_causal_backtest_applies_supermodel_block(monkeypatch) -> None:
    daily, intraday = _frames()
    monkeypatch.setattr(
        orchestrator,
        "_build_parallel_signal_result",
        lambda *args, **kwargs: (
            _signal(confidence=0.1),
            "parallel single-source (v2.5)",
            {"signal_mode": "parallel", "consensus": "BUY", "is_half_size": True},
        ),
    )
    settings = Settings()
    settings.app.signal_mode = "parallel"

    result = runner._run_symbol_backtest("AAPL", daily, intraday, settings)

    assert result["trades"] == 0


def test_causal_backtest_halves_single_source_position(monkeypatch) -> None:
    daily, intraday = _frames()
    monkeypatch.setattr(
        orchestrator,
        "_build_parallel_signal_result",
        lambda *args, **kwargs: (
            _signal(),
            "parallel single-source (v2.5)",
            {"signal_mode": "parallel", "consensus": "BUY", "is_half_size": True},
        ),
    )
    settings = Settings()
    settings.app.signal_mode = "parallel"

    result = runner._run_symbol_backtest("AAPL", daily, intraday, settings)

    assert result["trades"] == 1
    assert result["net_pnl"] == 38.0
