from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from trading_bot.config.settings import Settings
from trading_bot.models.signal import TradeSignal
from trading_bot.runtime import orchestrator


def _signal(confidence: float, tag: str) -> TradeSignal:
    return TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=98.0,
        profit_target=104.0,
        risk_reward_ratio=2.0,
        confidence=confidence,
        reasons=[tag],
        strategy_tag=tag,
        timestamp=datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc),
    )


def _daily_frame() -> pd.DataFrame:
    closes = [80.0 + index * 0.4 for index in range(60)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-04-01", periods=60, freq="D", tz="UTC"),
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [1_000_000] * 60,
        }
    )


def _intraday_frame() -> pd.DataFrame:
    closes = [99.0 + index * 0.05 for index in range(20)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-07-10 13:30", periods=20, freq="5min", tz="UTC"),
            "open": closes,
            "high": [value + 0.2 for value in closes],
            "low": [value - 0.2 for value in closes],
            "close": closes,
            "volume": [1000] * 20,
        }
    )


def test_build_signal_result_dispatches_parallel_mode(monkeypatch) -> None:
    expected = (_signal(0.8, "v3"), "parallel", {"signal_mode": "parallel"})
    monkeypatch.setattr(orchestrator, "_build_parallel_signal_result", lambda *args, **kwargs: expected)
    settings = Settings()
    settings.app.signal_mode = "parallel"

    assert orchestrator._build_signal_result("AAPL", settings) == expected


def test_parallel_frame_path_combines_two_buy_votes_without_fetching(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator.market_data,
        "fetch_and_validate_bars",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network fetch used")),
    )
    monkeypatch.setattr(
        orchestrator,
        "generate_recent_signal_with_reason",
        lambda *args, **kwargs: (_signal(0.7, "v2.5"), "buy"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_v3_signal_result",
        lambda *args, **kwargs: (_signal(0.8, "v3"), "buy", {"v3_total_score": 9.0}),
    )
    settings = Settings()
    settings.app.signal_mode = "parallel"
    settings.strategy.use_v3_signals = True

    signal, reason, details = orchestrator._build_parallel_signal_result(
        "AAPL",
        settings,
        daily_frame=_daily_frame(),
        intraday_frame=_intraday_frame(),
        hourly_frame=None,
    )

    assert signal is not None
    assert signal.confidence == 0.8
    assert reason == "parallel consensus (2/2)"
    assert details["consensus"] == "BUY"
    assert details["consensus_count"] == 2
    assert details["is_full_size"] is True


def test_parallel_frame_path_marks_single_buy_half_size(monkeypatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "generate_recent_signal_with_reason",
        lambda *args, **kwargs: (_signal(0.7, "v2.5"), "buy"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_build_v3_signal_result",
        lambda *args, **kwargs: (None, "hold", {}),
    )
    settings = Settings()
    settings.app.signal_mode = "parallel"
    settings.strategy.use_v3_signals = True

    signal, reason, details = orchestrator._build_parallel_signal_result(
        "AAPL",
        settings,
        daily_frame=_daily_frame(),
        intraday_frame=_intraday_frame(),
        hourly_frame=None,
    )

    assert signal is not None
    assert reason == "parallel single-source (v2.5)"
    assert details["consensus"] == "BUY"
    assert details["is_half_size"] is True
