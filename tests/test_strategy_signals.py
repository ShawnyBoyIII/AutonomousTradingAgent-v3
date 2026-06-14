import pytest
import pandas as pd

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.daily_filter import is_bullish_daily_regime
from trading_bot.strategy.intraday_signal_engine import generate_signal
from trading_bot.strategy.setup_rules import detect_intraday_breakout


def test_daily_regime_true_when_price_above_trend() -> None:
    frame = pd.DataFrame(
        {
            "close": [100, 102, 104],
            "ema_20": [99, 100, 101],
            "sma_50": [98, 99, 100],
        }
    )

    assert is_bullish_daily_regime(frame) is True


def test_daily_regime_false_for_empty_or_missing_columns() -> None:
    assert is_bullish_daily_regime(pd.DataFrame()) is False
    assert is_bullish_daily_regime(pd.DataFrame({"close": [1], "ema_20": [1]})) is False


def test_intraday_breakout_detects_range_break() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
        }
    )

    breakout = detect_intraday_breakout(frame)
    assert breakout is True


def test_intraday_breakout_false_for_missing_columns_or_short_frame() -> None:
    short_frame = pd.DataFrame(
        {
            "close": [100.0, 100.2],
            "high": [100.1, 100.3],
            "volume": [1000, 1100],
            "volume_avg_5": [1000, 1000],
        }
    )
    missing_column_frame = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
        }
    )

    assert detect_intraday_breakout(short_frame) is False
    assert detect_intraday_breakout(missing_column_frame) is False


def test_intraday_breakout_false_for_null_latest_breakout_inputs() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, pd.NA],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, pd.NA],
        }
    )

    assert detect_intraday_breakout(frame) is False


def test_intraday_breakout_false_for_incomplete_lookback_range() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, pd.NA, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
        }
    )

    assert detect_intraday_breakout(frame) is False


def test_generate_signal_returns_buy_candidate_on_bullish_breakout() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0, 102.0, 104.0],
            "ema_20": [99.0, 100.0, 101.0],
            "sma_50": [98.0, 99.0, 100.0],
        },
        index=pd.to_datetime(["2026-06-13 09:30:00", "2026-06-13 09:35:00", "2026-06-13 09:40:00"]),
    )
    intraday = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
            "low": [99.8, 100.0, 99.91234, 100.1, 100.4],
        },
        index=pd.to_datetime(
            [
                "2026-06-13 10:00:00",
                "2026-06-13 10:05:00",
                "2026-06-13 10:10:00",
                "2026-06-13 10:15:00",
                "2026-06-13 10:20:00",
            ]
        ),
    )

    signal = generate_signal("AAPL", daily, intraday)

    assert signal is not None
    assert isinstance(signal, TradeSignal)
    assert signal.ticker == "AAPL"
    assert signal.action == "BUY"
    assert signal.timeframe == "intraday"
    assert signal.entry_price > signal.stop_loss
    assert signal.profit_target > signal.entry_price
    assert signal.risk_reward_ratio == pytest.approx(
        round((signal.profit_target - signal.entry_price) / (signal.entry_price - signal.stop_loss), 6)
    )
    assert "bullish daily regime" in signal.reasons
    assert "intraday breakout" in signal.reasons
    assert signal.timestamp == pd.Timestamp("2026-06-13 10:20:00")


def test_generate_signal_returns_none_without_datetime_index() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0, 102.0, 104.0],
            "ema_20": [99.0, 100.0, 101.0],
            "sma_50": [98.0, 99.0, 100.0],
        }
    )
    intraday = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4],
        }
    )

    assert generate_signal("AAPL", daily, intraday) is None


def test_generate_signal_returns_none_for_missing_numeric_values() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0, 102.0, 104.0],
            "ema_20": [99.0, 100.0, 101.0],
            "sma_50": [98.0, 99.0, 100.0],
        },
        index=pd.to_datetime(["2026-06-13 09:30:00", "2026-06-13 09:35:00", "2026-06-13 09:40:00"]),
    )
    intraday = pd.DataFrame(
        {
            "close": [100.0, 100.2, 100.1, 100.3, 101.0],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1],
            "volume": [1000, 1100, 950, 1050, 2500],
            "volume_avg_5": [1000, 1000, 1000, 1000, 1000],
            "low": [pd.NA, pd.NA, pd.NA, pd.NA, float("nan")],
        },
        index=pd.to_datetime(
            [
                "2026-06-13 10:00:00",
                "2026-06-13 10:05:00",
                "2026-06-13 10:10:00",
                "2026-06-13 10:15:00",
                "2026-06-13 10:20:00",
            ]
        ),
    )

    assert generate_signal("AAPL", daily, intraday) is None
