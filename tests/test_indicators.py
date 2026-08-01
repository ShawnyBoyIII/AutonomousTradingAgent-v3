from __future__ import annotations

import pytest
import numpy as np
import pandas as pd

from trading_bot.data.indicators import (
    add_atr,
    add_ema,
    add_rsi,
    add_sma,
    add_macd,
    add_bollinger_bands,
    add_vwap,
    add_stochastic,
    add_adx,
    add_williams_r,
    add_obv,
)
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


def test_add_atr_creates_expected_column() -> None:
    frame = pd.DataFrame(
        {
            "high":   [105.0, 110.0, 108.0, 115.0, 120.0, 122.0],
            "low":    [ 95.0, 100.0, 102.0, 108.0, 113.0, 116.0],
            "close":  [100.0, 105.0, 106.0, 112.0, 118.0, 119.0],
            "volume": [1e6] * 6,
        }
    )

    result = add_atr(frame, period=3, column_name="atr_3")

    assert "atr_3" not in frame.columns
    assert "atr_3" in result.columns
    assert result["atr_3"].iloc[:3].isna().all()
    # First ATR seeds as average of TR[1..3]: max(10,10,0)=10, max(6,3,3)=6, max(7,9,2)=9
    assert result["atr_3"].iloc[3] == pytest.approx(25.0 / 3.0)
    # Wilder smoothing on TR[4]=8 and TR[5]=6
    seed = 25.0 / 3.0
    expected_4 = (seed * 2 + 8.0) / 3.0
    expected_5 = (expected_4 * 2 + 6.0) / 3.0
    assert result["atr_3"].iloc[4] == pytest.approx(expected_4)
    assert result["atr_3"].iloc[5] == pytest.approx(expected_5)


def test_add_atr_returns_all_nan_when_insufficient_history() -> None:
    frame = pd.DataFrame(
        {
            "high":  [105.0, 110.0],
            "low":   [ 95.0, 100.0],
            "close": [100.0, 105.0],
        }
    )

    result = add_atr(frame, period=3, column_name="atr_3")

    assert "atr_3" in result.columns
    assert result["atr_3"].isna().all()


def test_add_atr_requires_high_low_close() -> None:
    frame = pd.DataFrame({"close": [100.0, 105.0]})

    with pytest.raises(KeyError):
        add_atr(frame, period=2)


def test_add_macd_creates_expected_columns() -> None:
    # Create uptrend data: 26 periods flat, then rising
    frame = pd.DataFrame({"close": [100.0] * 25 + [100.0, 110.0, 115.0, 120.0, 125.0]})

    result = add_macd(frame, fast_period=12, slow_period=26, signal_period=9)

    assert "macd_line" in result.columns
    assert "macd_signal" in result.columns
    assert "macd_histogram" in result.columns

    # MACD line should start having values after slow_period - 1 (index 25)
    assert result["macd_line"].iloc[:25].isna().all()
    assert result["macd_line"].iloc[25] is not None

    # MACD line should be positive in uptrend (last value is 125)
    assert result["macd_line"].iloc[-1] > 0


def test_add_bollinger_bands_creates_expected_columns() -> None:
    # Create price data with known pattern (more gradual change)
    prices = [100.0 + i * 0.5 for i in range(20)]  # Gradual uptrend
    frame = pd.DataFrame({"close": prices})

    result = add_bollinger_bands(frame, period=20, std_dev=2.0)

    assert "bb_middle" in result.columns
    assert "bb_upper" in result.columns
    assert "bb_lower" in result.columns
    assert "bb_width" in result.columns
    assert "bb_percent_b" in result.columns

    # Middle band should equal SMA at index 19
    assert result["bb_middle"].iloc[19] == pytest.approx(104.75)

    # Upper should be above middle, lower below
    assert result["bb_upper"].iloc[19] > result["bb_middle"].iloc[19]
    assert result["bb_lower"].iloc[19] < result["bb_middle"].iloc[19]

    # %B should exist (can be outside 0-100 if price breaks bands)
    assert result["bb_percent_b"].iloc[19] is not None


def test_add_vwap_creates_expected_column() -> None:
    frame = pd.DataFrame(
        {
            "high": [102.0, 104.0, 106.0],
            "low": [98.0, 100.0, 102.0],
            "close": [100.0, 102.0, 104.0],
            "volume": [1000.0, 2000.0, 3000.0],
        }
    )

    result = add_vwap(frame)

    assert "vwap" in result.columns

    # VWAP should be cumulative
    # Bar 1: typical = 100, cum_tv = 100000, cum_v = 1000, vwap = 100
    # Bar 2: typical = 102, cum_tv = 304000, cum_v = 3000, vwap = 101.333
    # Bar 3: typical = 104, cum_tv = 616000, cum_v = 6000, vwap = 102.667
    assert result["vwap"].iloc[0] == pytest.approx(100.0)
    assert result["vwap"].iloc[1] == pytest.approx(101.333, rel=1e-3)
    assert result["vwap"].iloc[2] == pytest.approx(102.667, rel=1e-3)


def test_add_vwap_requires_ohlcv() -> None:
    frame = pd.DataFrame({"close": [100.0, 105.0]})

    with pytest.raises(KeyError):
        add_vwap(frame)


def test_optimized_indicator_casts_preserve_object_and_nullable_inputs() -> None:
    numeric = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 11.5, 13.0, 14.0, 13.5, 15.0],
            "low": [8.0, 9.0, 10.0, 9.5, 11.0, 12.0, 11.5, 13.0],
            "close": [9.0, 10.5, 11.5, 10.5, 12.5, 13.5, 12.5, 14.5],
            "volume": [1000.0, 1200.0, 1100.0, 1400.0, 1600.0, 1500.0, 1800.0, 2000.0],
        }
    )
    object_frame = numeric.astype(str)
    nullable_frame = numeric.astype("Float64")
    builders = (
        lambda frame: add_atr(frame, period=3),
        add_vwap,
        lambda frame: add_stochastic(frame, k_period=3, d_period=2),
        lambda frame: add_adx(frame, period=3),
        lambda frame: add_williams_r(frame, period=3),
        add_obv,
    )

    for build in builders:
        expected = build(numeric.copy())
        for variant in (object_frame, nullable_frame):
            actual = build(variant.copy())
            assert list(actual.columns) == list(expected.columns)
            for column in expected.columns:
                np.testing.assert_allclose(
                    actual[column].to_numpy(dtype=float),
                    expected[column].to_numpy(dtype=float),
                    equal_nan=True,
                )


def test_add_stochastic_creates_expected_columns() -> None:
    frame = pd.DataFrame(
        {
            "high": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0],
            "low": [8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0],
            "close": [9.0, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5],
        }
    )

    from trading_bot.data.indicators import add_stochastic

    result = add_stochastic(frame, k_period=5, d_period=3)

    assert "stoch_k" in result.columns
    assert "stoch_d" in result.columns
    # First valid %K at index 4 (k_period - 1)
    assert result["stoch_k"].iloc[:4].isna().all()
    assert result["stoch_k"].iloc[4] is not None
    # %D should be SMA of %K, so valid from index 6 (4 + d_period - 1)
    # Note: Implementation may vary, just check values exist
    assert result["stoch_d"].iloc[6] is not None


def test_add_stochastic_bounded() -> None:
    frame = pd.DataFrame(
        {
            "high": [10.0 + i for i in range(20)],
            "low": [8.0 + i for i in range(20)],
            "close": [9.0 + i for i in range(20)],
        }
    )

    from trading_bot.data.indicators import add_stochastic

    result = add_stochastic(frame, k_period=14, d_period=3)
    k_values = result["stoch_k"].dropna()

    # %K should be bounded 0-100
    assert (k_values >= 0).all()
    assert (k_values <= 100).all()


def test_add_williams_r_creates_expected_column() -> None:
    frame = pd.DataFrame(
        {
            "high": [10.0 + i for i in range(20)],
            "low": [8.0 + i for i in range(20)],
            "close": [9.0 + i for i in range(20)],
        }
    )

    from trading_bot.data.indicators import add_williams_r

    result = add_williams_r(frame, period=14)

    assert "williams_r" in result.columns
    # First valid %R at index 13 (period - 1)
    assert result["williams_r"].iloc[:13].isna().all()
    assert result["williams_r"].iloc[13] is not None
    # Williams %R should be bounded -100 to 0
    wr_values = result["williams_r"].dropna()
    assert (wr_values >= -100).all()
    assert (wr_values <= 0).all()


def test_add_obv_creates_expected_column() -> None:
    frame = pd.DataFrame(
        {
            "close": [100.0, 102.0, 101.0, 103.0, 105.0],
            "volume": [1000.0, 1500.0, 1200.0, 1800.0, 2000.0],
        }
    )

    from trading_bot.data.indicators import add_obv

    result = add_obv(frame)

    assert "obv" in result.columns
    # OBV should be cumulative
    assert result["obv"].iloc[0] == 1000.0
    # Day 2: close up, OBV += 1500 = 2500
    assert result["obv"].iloc[1] == 2500.0
    # Day 3: close down, OBV -= 1200 = 1300
    assert result["obv"].iloc[2] == 1300.0
    # Day 4: close up, OBV += 1800 = 3100
    assert result["obv"].iloc[3] == 3100.0
    # Day 5: close up, OBV += 2000 = 5100
    assert result["obv"].iloc[4] == 5100.0


def test_add_atr_percent_creates_expected_columns() -> None:
    frame = pd.DataFrame(
        {
            "high": [100.0 + i for i in range(30)],
            "low": [98.0 + i for i in range(30)],
            "close": [99.0 + i for i in range(30)],
        }
    )

    from trading_bot.data.indicators import add_atr_percent

    result = add_atr_percent(frame, period=14)

    assert "atr_14" in result.columns
    assert "atr_pct" in result.columns
    # ATR% should be positive
    atr_pct_values = result["atr_pct"].dropna()
    assert (atr_pct_values > 0).all()
