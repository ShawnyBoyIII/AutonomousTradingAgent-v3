from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.config.settings import RiskSettings, Settings
from trading_bot.data.validation import ValidationResult
from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.signal_quality import (
    adapt_signal_to_volatility_regime,
    evaluate_entry_timing,
    evaluate_signal_quality,
    evaluate_timeframe_alignment,
)


ET = ZoneInfo("America/New_York")


def _daily_frame(close: float = 105.0, ema: float = 102.0, sma: float = 100.0) -> pd.DataFrame:
    rows = 60
    return pd.DataFrame(
        {
            "open": [close - 1.0] * rows,
            "high": [close + 1.0] * rows,
            "low": [close - 2.0] * rows,
            "close": [close] * rows,
            "volume": [1_000_000] * rows,
            "ema_20": [ema] * rows,
            "sma_50": [sma] * rows,
            "atr_14": [2.0] * rows,
        },
        index=pd.date_range("2026-01-01", periods=rows, freq="1d"),
    )


def _intraday_frame(
    *,
    closes: list[float] | None = None,
    volume: float = 1_400.0,
    avg_volume: float = 1_000.0,
    latest_range: float = 0.4,
    timestamp: datetime | None = None,
) -> pd.DataFrame:
    closes = closes or [100.0, 100.1, 100.2, 100.3, 100.5, 100.8]
    rows = len(closes)
    timestamp = timestamp or datetime(2026, 7, 2, 10, 0, tzinfo=ET)
    index = pd.date_range(end=timestamp, periods=rows, freq="5min")
    highs = [value + 0.2 for value in closes]
    lows = [value - 0.2 for value in closes]
    highs[-1] = closes[-1] + latest_range / 2
    lows[-1] = closes[-1] - latest_range / 2
    return pd.DataFrame(
        {
            "open": [value - 0.05 for value in closes],
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [avg_volume] * (rows - 1) + [volume],
            "volume_avg_5": [avg_volume] * rows,
            "atr_14": [1.0] * rows,
        },
        index=index,
    )


def _hourly_frame(aligned: bool = True) -> pd.DataFrame:
    closes = [100.0, 101.0, 102.0, 103.0] if aligned else [103.0, 102.0, 101.0, 100.0]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [10_000] * len(closes),
            "ema_20": [99.0] * len(closes),
            "sma_50": [98.0] * len(closes),
        },
        index=pd.date_range("2026-07-02 09:00", periods=len(closes), freq="1h", tz=ET),
    )


def _signal(quality: str = "GREEN") -> TradeSignal:
    return TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.8,
        stop_loss=98.8,
        profit_target=104.8,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["intraday breakout"],
        strategy_tag="test",
        timestamp=datetime(2026, 7, 2, 10, 0, tzinfo=ET),
        quality=quality,
    )


def test_timeframe_alignment_passes_with_daily_and_5m_when_hourly_missing() -> None:
    verdict = evaluate_timeframe_alignment(
        daily_frame=_daily_frame(),
        hourly_frame=None,
        intraday_frame=_intraday_frame(),
    )

    assert verdict.passed is True
    assert verdict.aligned_count == 2
    assert verdict.daily is True
    assert verdict.hourly is False
    assert verdict.five_min is True
    assert "hourly unavailable" in verdict.reasons


def test_timeframe_alignment_rejects_when_only_daily_aligns() -> None:
    verdict = evaluate_timeframe_alignment(
        daily_frame=_daily_frame(),
        hourly_frame=_hourly_frame(aligned=False),
        intraday_frame=_intraday_frame(closes=[100, 99.8, 99.7, 99.6, 99.5, 99.4]),
    )

    assert verdict.passed is False
    assert verdict.aligned_count == 1
    assert "requires 2 aligned timeframes" in verdict.reasons


def test_entry_timing_rejects_breakout_without_volume_confirmation() -> None:
    verdict = evaluate_entry_timing(
        intraday_frame=_intraday_frame(volume=1_100.0, avg_volume=1_000.0),
        signal=_signal(),
        setup_name="intraday breakout",
    )

    assert verdict.passed is False
    assert "breakout volume below 1.2x average" in verdict.reasons


def test_entry_timing_rejects_panic_bar() -> None:
    verdict = evaluate_entry_timing(
        intraday_frame=_intraday_frame(latest_range=2.0),
        signal=_signal(),
        setup_name="intraday breakout",
    )

    assert verdict.passed is False
    assert "5m range exceeds 3x average range" in verdict.reasons


def test_entry_timing_rejects_yellow_without_two_bar_confirmation() -> None:
    verdict = evaluate_entry_timing(
        intraday_frame=_intraday_frame(closes=[100, 101, 100.5, 100.4, 100.3, 100.2]),
        signal=_signal(quality="YELLOW"),
        setup_name="intraday momentum continuation",
        quality="YELLOW",
    )

    assert verdict.passed is False
    assert "YELLOW signal lacks 2-bar confirmation" in verdict.reasons


def test_signal_quality_combines_alignment_and_entry_timing() -> None:
    verdict = evaluate_signal_quality(
        daily_frame=_daily_frame(),
        hourly_frame=_hourly_frame(),
        intraday_frame=_intraday_frame(),
        signal=_signal(),
        setup_name="intraday breakout",
        quality="GREEN",
    )

    assert verdict.passed is True
    assert verdict.alignment.aligned_count == 3
    assert verdict.entry_timing.preferred_window is True


def test_adaptive_stop_target_uses_high_volatility_geometry() -> None:
    daily = _daily_frame()
    daily["atr_14"] = [1.0] * 40 + [5.0] * 20
    signal = _signal()

    adapted = adapt_signal_to_volatility_regime(
        signal,
        daily,
        _intraday_frame(),
        RiskSettings(min_stop_distance_pct=0.0),
    )

    assert adapted.stop_loss == 97.05
    assert adapted.profit_target == 110.175
    assert round(adapted.risk_reward_ratio, 2) == 2.5
    assert any("adaptive regime=high_volatility" in reason for reason in adapted.reasons)


def test_serial_rl_signal_is_gated_by_phase1_quality(monkeypatch) -> None:
    from trading_bot.data import market_data
    from trading_bot.runtime import orchestrator

    settings = Settings(rl={"enabled": True})
    rl_signal = _signal()

    monkeypatch.setattr(
        orchestrator,
        "_build_rl_signal_result",
        lambda symbol, settings: (rl_signal, "rl approved", {"rl_action": 1}),
    )

    def fake_fetch(symbol, period, interval, settings):
        if interval == "1d":
            return _daily_frame(), ValidationResult(valid=True, reason="ok")
        if interval == "1h":
            return _hourly_frame(), ValidationResult(valid=True, reason="ok")
        return _intraday_frame(), ValidationResult(valid=True, reason="ok")

    monkeypatch.setattr(market_data, "fetch_and_validate_bars", fake_fetch)

    signal, reason, details = orchestrator._build_signal_result("AAPL", settings)

    assert signal is not None
    assert reason == "rl approved"
    assert details["signal_quality_passed"] is True
    assert details["mtf_aligned"] >= 2
    assert "adaptive_stop_loss" in details
