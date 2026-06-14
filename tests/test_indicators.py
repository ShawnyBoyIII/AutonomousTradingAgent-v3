from __future__ import annotations

import pytest
import pandas as pd

from trading_bot.data.indicators import add_ema, add_rsi, add_sma
from trading_bot.data.market_data import normalize_ohlcv_frame


def test_add_ema_creates_expected_column() -> None:
    frame = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0]})

    result = add_ema(frame, period=3, column_name="ema_3")

    assert "ema_3" not in frame.columns
    assert "ema_3" in result.columns
    assert result["ema_3"].iloc[:2].isna().all()
    assert result["ema_3"].iloc[2] == pytest.approx(11.0)
    assert result["ema_3"].iloc[3] == pytest.approx(12.0)
    assert result["ema_3"].iloc[4] == pytest.approx(13.0)


def test_add_rsi_keeps_values_bounded() -> None:
    frame = pd.DataFrame(
        {"close": [44.0, 44.15, 43.9, 44.35, 44.8, 44.5, 45.0, 45.4, 45.1]}
    )

    result = add_rsi(frame, period=3)

    bounded_values = result["rsi_3"].dropna()

    assert not bounded_values.empty
    assert bounded_values.between(0.0, 100.0).all()
    assert bounded_values.iloc[0] == pytest.approx(70.58823529411771)
    assert bounded_values.iloc[-1] == pytest.approx(60.8927890118277)


def test_add_sma_creates_expected_column() -> None:
    frame = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0]})

    result = add_sma(frame, period=3, column_name="sma_3")

    assert "sma_3" not in frame.columns
    assert "sma_3" in result.columns
    assert result["sma_3"].iloc[:2].isna().all()
    assert result["sma_3"].iloc[2] == pytest.approx(11.0)
    assert result["sma_3"].iloc[3] == pytest.approx(12.0)
    assert result["sma_3"].iloc[4] == pytest.approx(13.0)


def test_normalize_ohlcv_frame_standardizes_yahoo_columns() -> None:
    frame = pd.DataFrame(
        {
            "Open": [100.0, 101.0],
            "High": [102.0, 103.0],
            "Low": [99.0, 100.0],
            "Close": [101.5, 102.5],
            "Adj Close": [101.0, 102.0],
            "Volume": [1_000_000, 1_100_000],
        },
        index=pd.to_datetime(["2026-06-13 10:00:00", "2026-06-13 10:01:00"]),
    )

    result = normalize_ohlcv_frame(frame)

    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert result["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist() == [
        "2026-06-13 10:00:00",
        "2026-06-13 10:01:00",
    ]
    assert result["open"].tolist() == [100.0, 101.0]
    assert result["volume"].tolist() == [1_000_000, 1_100_000]
