"""Tests for strategy daily_signal_engine module (70 lines)."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.daily_signal_engine import generate_daily_signal


def _build_frame(
    *,
    n: int = 21,
    today_close: float = 105.0,
    yesterday_high: float = 100.0,
    yesterday_low: float = 90.0,
    today_volume: float = 2000.0,
    base_volume: float = 1000.0,
    atr: float | None = 2.0,
) -> pd.DataFrame:
    """Build a daily OHLCV frame with a bullish regime ending at the last row."""
    closes = [100.0] * (n - 1) + [today_close]
    ema = [98.0] * (n - 1) + [100.0]
    sma = [95.0] * (n - 1) + [97.0]
    highs = [yesterday_high] * n
    lows = [yesterday_low] * n
    volumes = [base_volume] * (n - 1) + [today_volume]
    data = {
        "close": closes,
        "ema_20": ema,
        "sma_50": sma,
        "high": highs,
        "low": lows,
        "volume": volumes,
    }
    if atr is not None:
        data["atr_14"] = [atr] * n
    timestamps = [datetime(2024, 1, 1) + pd.Timedelta(days=i) for i in range(n)]
    data["timestamp"] = timestamps
    return pd.DataFrame(data)


class TestGenerateDailySignal:
    def test_returns_none_when_index_below_warmup(self) -> None:
        frame = _build_frame()
        assert generate_daily_signal("AAPL", frame, index=5) is None

    def test_returns_none_when_not_bullish_regime(self) -> None:
        frame = _build_frame()
        # Break the regime on the last row: close below ema
        frame.loc[frame.index[-1], "close"] = 50.0
        # Ensure no accidental breakout signal still needed; close below yesterday high
        frame.loc[frame.index[-1], "high"] = 200.0
        assert generate_daily_signal("AAPL", frame, index=20) is None

    def test_returns_none_when_no_breakout(self) -> None:
        frame = _build_frame(today_close=99.0, yesterday_high=100.0)
        # Regime still bullish (99 > 100? no). Need regime bullish but close <= high.
        # Override ema/sma so regime is bullish with close=99
        frame.loc[frame.index[-1], "ema_20"] = 98.0
        frame.loc[frame.index[-1], "sma_50"] = 95.0
        result = generate_daily_signal("AAPL", frame, index=20)
        assert result is None

    def test_returns_none_when_no_volume_surge(self) -> None:
        # today_volume not >= 1.5 * avg
        frame = _build_frame(today_volume=1200.0, base_volume=1000.0)
        result = generate_daily_signal("AAPL", frame, index=20)
        assert result is None

    def test_signal_with_atr(self) -> None:
        frame = _build_frame(today_close=105.0, atr=2.0)
        signal = generate_daily_signal("AAPL", frame, index=20)
        assert isinstance(signal, TradeSignal)
        assert signal.ticker == "AAPL"
        assert signal.timeframe == "daily"
        assert signal.action == "BUY"
        assert signal.entry_price == 105.0
        # stop_distance = atr * 2 = 4.0
        assert signal.stop_loss == pytest.approx(101.0)
        assert signal.profit_target == pytest.approx(113.0)
        assert signal.risk_reward_ratio == pytest.approx(2.0)
        assert signal.confidence == pytest.approx(0.7)
        assert signal.reasons == ["daily_breakout", "volume_surge", "bullish_regime"]
        assert signal.strategy_tag == "daily_breakout_v1"
        assert signal.timestamp == frame.iloc[20]["timestamp"]

    def test_signal_without_atr_uses_yesterday_range(self) -> None:
        frame = _build_frame(
            today_close=105.0,
            yesterday_high=100.0,
            yesterday_low=90.0,
            atr=None,
        )
        signal = generate_daily_signal("AAPL", frame, index=20)
        assert isinstance(signal, TradeSignal)
        # stop_distance = yesterday high - low = 10.0
        assert signal.stop_loss == pytest.approx(95.0)
        assert signal.profit_target == pytest.approx(125.0)
        assert signal.risk_reward_ratio == pytest.approx(2.0)

    def test_signal_uses_index_as_timestamp_when_column_missing(self) -> None:
        frame = _build_frame()
        frame = frame.drop(columns=["timestamp"])
        frame.index = pd.date_range("2024-01-01", periods=len(frame))
        signal = generate_daily_signal("AAPL", frame, index=20)
        assert isinstance(signal, TradeSignal)
        assert signal.timestamp == frame.index[20]

    def test_atr_zero_falls_back_to_range(self) -> None:
        frame = _build_frame(today_close=105.0, atr=0.0)
        # atr_14 present but == 0 -> fallback to yesterday range
        signal = generate_daily_signal("AAPL", frame, index=20)
        assert isinstance(signal, TradeSignal)
        assert signal.stop_loss == pytest.approx(95.0)
        assert signal.profit_target == pytest.approx(125.0)