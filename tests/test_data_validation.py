import pandas as pd
import pytest

from trading_bot.data import market_data
from trading_bot.config.settings import MarketDataSettings
from trading_bot.data.validation import (
    ValidationResult,
    validate_market_data,
    validate_ohlc_coherence,
    validate_price_sanity,
    validate_volume_sanity,
)


def test_validate_price_sanity_accepts_valid_prices() -> None:
    frame = pd.DataFrame({
        "close": [100.0, 101.0, 102.0],
    })
    result = validate_price_sanity(frame)
    assert result.valid is True


def test_validate_price_sanity_rejects_zero_prices() -> None:
    frame = pd.DataFrame({
        "close": [100.0, 0.0, 102.0],
    })
    result = validate_price_sanity(frame)
    assert result.valid is False
    assert "non-positive" in result.reason


def test_validate_price_sanity_rejects_negative_prices() -> None:
    frame = pd.DataFrame({
        "close": [100.0, -5.0, 102.0],
    })
    result = validate_price_sanity(frame)
    assert result.valid is False
    assert "non-positive" in result.reason


def test_validate_price_sanity_rejects_excessive_jump() -> None:
    frame = pd.DataFrame({
        "close": [100.0, 100.5, 1500.0],  # 15x jump
    })
    result = validate_price_sanity(frame, max_price_jump_pct=1000.0)
    assert result.valid is False
    assert "price jump" in result.reason


def test_validate_price_sanity_allows_normal_volatility() -> None:
    frame = pd.DataFrame({
        "close": [100.0, 105.0, 110.0],  # 5% and 4.8% jumps
    })
    result = validate_price_sanity(frame, max_price_jump_pct=1000.0)
    assert result.valid is True


def test_validate_price_sanity_rejects_empty_frame() -> None:
    frame = pd.DataFrame({"close": []})
    result = validate_price_sanity(frame)
    assert result.valid is False
    assert "empty" in result.reason


def test_validate_price_sanity_rejects_missing_column() -> None:
    frame = pd.DataFrame({"open": [100.0, 101.0]})
    result = validate_price_sanity(frame)
    assert result.valid is False
    assert "missing" in result.reason


def test_validate_ohlc_coherence_accepts_valid_bar() -> None:
    frame = pd.DataFrame({
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
    })
    result = validate_ohlc_coherence(frame)
    assert result.valid is True


def test_validate_ohlc_coherence_rejects_high_below_low() -> None:
    frame = pd.DataFrame({
        "high": [100.0],
        "low": [102.0],  # low > high
        "close": [101.0],
    })
    result = validate_ohlc_coherence(frame)
    assert result.valid is False
    assert "high < low" in result.reason


def test_validate_ohlc_coherence_rejects_close_above_high() -> None:
    frame = pd.DataFrame({
        "high": [100.0],
        "low": [98.0],
        "close": [105.0],  # close > high
    })
    result = validate_ohlc_coherence(frame)
    assert result.valid is False
    assert "close > high" in result.reason


def test_validate_ohlc_coherence_rejects_close_below_low() -> None:
    frame = pd.DataFrame({
        "high": [100.0],
        "low": [98.0],
        "close": [95.0],  # close < low
    })
    result = validate_ohlc_coherence(frame)
    assert result.valid is False
    assert "close < low" in result.reason


def test_validate_ohlc_coherence_rejects_open_above_high() -> None:
    frame = pd.DataFrame({
        "open": [105.0],  # open > high
        "high": [100.0],
        "low": [98.0],
        "close": [99.0],
    })
    result = validate_ohlc_coherence(frame)
    assert result.valid is False
    assert "open > high" in result.reason


def test_validate_ohlc_coherence_rejects_nan_values() -> None:
    frame = pd.DataFrame({
        "high": [100.0, float("nan")],
        "low": [98.0, 97.0],
        "close": [99.0, 98.0],
    })
    result = validate_ohlc_coherence(frame)
    assert result.valid is False
    assert "NaN" in result.reason


def test_validate_volume_sanity_accepts_valid_volume() -> None:
    frame = pd.DataFrame({
        "volume": [1000, 1100, 1200],
    })
    result = validate_volume_sanity(frame)
    assert result.valid is True


def test_validate_volume_sanity_rejects_negative_volume() -> None:
    frame = pd.DataFrame({
        "volume": [1000, -100, 1200],
    })
    result = validate_volume_sanity(frame)
    assert result.valid is False
    assert "negative" in result.reason


def test_validate_volume_sanity_rejects_excessive_jump() -> None:
    frame = pd.DataFrame({
        "volume": [1000, 1001, 15000],  # 15x jump
    })
    result = validate_volume_sanity(frame, max_volume_jump_pct=1000.0)
    assert result.valid is False
    assert "volume jump" in result.reason


def test_validate_volume_sanity_skips_if_no_volume_column() -> None:
    frame = pd.DataFrame({
        "close": [100.0, 101.0],
    })
    result = validate_volume_sanity(frame)
    assert result.valid is True


def test_validate_market_data_runs_all_checks() -> None:
    frame = pd.DataFrame({
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
        "volume": [1000, 1100],
    })
    result = validate_market_data(frame)
    assert result.valid is True


def test_validate_market_data_rejects_insufficient_bars() -> None:
    frame = pd.DataFrame({
        "close": [100.0],
    })
    result = validate_market_data(frame, min_bars=2)
    assert result.valid is False
    assert "insufficient bars" in result.reason


def test_validate_market_data_stops_on_first_failure() -> None:
    frame = pd.DataFrame({
        "close": [100.0, 0.0],  # Invalid: zero price
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
    })
    result = validate_market_data(frame)
    assert result.valid is False
    assert "non-positive" in result.reason


def test_fetch_and_validate_bars_passes_provider_settings_to_fetch(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch_bars(symbol: str, period: str, interval: str, **kwargs) -> pd.DataFrame:
        captured["settings"] = kwargs.get("settings")
        return pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.0, 100.0],
                "close": [101.0, 102.0],
                "volume": [1000, 1100],
            }
        )

    monkeypatch.setattr(market_data, "fetch_bars", fake_fetch_bars)
    settings = MarketDataSettings(provider="alpaca", min_bars_for_signal=2)

    _, result = market_data.fetch_and_validate_bars("AAPL", "1y", "1d", settings=settings)

    assert result.valid is True
    assert captured["settings"] is settings
