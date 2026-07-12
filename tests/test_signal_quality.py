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
    # Default to *fresh* bar at 10:15 ET today; reflects a real intraday bar
    # that the A1 wall-clock fallback would treat as fresh. Tests that
    # want a stale-bar edge case can pass `timestamp=<old date>` explicitly.
    timestamp = timestamp or datetime.now(tz=ET).replace(
        hour=10, minute=15, second=0, microsecond=0
    ) - pd.Timedelta(minutes=5 * (rows - 1))  # noqa
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
    # Use a *fresh* timestamp inside the morning preferred window
    # (9:45–11:30 ET) so tests reflect the actual happy-path of the
    # entry-timing module, regardless of when tests are run. The 2026-07-02
    # fixture that was here is 6 days stale by now, which causes A1's
    # wall-clock fallback to land in the 11:57 dead zone between
    # windows — historically the test passed only by accident.
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
        timestamp=datetime.now(tz=ET).replace(hour=10, minute=15, second=0, microsecond=0),
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


def test_signal_quality_combines_alignment_and_entry_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin wall-clock to 10:15 ET today so _signal_timestamp returns a
    # timestamp inside the morning preferred window regardless of when
    # the test runs. Avoids the 4pm-after-hours false-positive that
    # makes `verdict.passed is True` time-of-day dependent.
    now_et = datetime.now(tz=ET).replace(hour=10, minute=15, second=0, microsecond=0)
    now_utc = now_et.astimezone(ZoneInfo("UTC"))
    from datetime import timezone as _tz

    class _FrozenDatetime:
        @classmethod
        def now(cls, tz: _tz | None = None) -> datetime:
            if tz is None:
                return now_utc.replace(tzinfo=None)
            return now_utc.astimezone(tz) if tz else now_utc.replace(tzinfo=None)

    monkeypatch.setattr("trading_bot.strategy.signal_quality.datetime", _FrozenDatetime)

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


def test_signal_timestamp_falls_back_to_wall_clock_on_stale_bar() -> None:
    """A1 (Tier 1, 2026-07-08): when the latest bar is from a previous
    trading session (or much older — the bar's date is *not* today),
    _signal_timestamp must NOT return that stale bar — it should return
    a fresh, tz-aware timestamp near wall-clock, so the avoid-window
    check evaluates against NOW, not against yesterday's close.
    """
    from datetime import timedelta
    from trading_bot.strategy.signal_quality import _signal_timestamp

    # 7 hours back — beyond the 6h intra-day tolerance but still "today"
    # in calendar terms; we test the wider tolerance.
    stale_ts = datetime.now(ET) - timedelta(hours=7)
    df = pd.DataFrame(
        {
            "timestamp": [stale_ts],
            "close": [100.0],
            "volume": [1000],
        }
    )
    result = _signal_timestamp(signal=None, frame=df)
    assert result is not None
    age_seconds = (datetime.now(ET) - result).total_seconds()
    assert age_seconds < 60, (
        f"timestamp should be near wall-clock when the bar is stale; got age={age_seconds:.0f}s"
    )
    assert result.tzinfo is not None, "returned timestamp must be tz-aware"


def test_signal_timestamp_uses_fresh_bar() -> None:
    """Counter-test: a fresh bar (<5 min old) should still be used as-is."""
    from datetime import timedelta
    from trading_bot.strategy.signal_quality import _signal_timestamp

    fresh_ts = datetime.now(ET) - timedelta(seconds=30)
    df = pd.DataFrame(
        {
            "timestamp": [fresh_ts],
            "close": [100.0],
            "volume": [1000],
        }
    )
    result = _signal_timestamp(signal=None, frame=df)
    assert result == fresh_ts, (
        "fresh bar (<5min) should be returned verbatim"
    )


