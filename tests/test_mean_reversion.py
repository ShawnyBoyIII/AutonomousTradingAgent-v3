"""Tests for mean reversion signal detection."""

from __future__ import annotations

import pandas as pd

from trading_bot.strategy.mean_reversion import (
    detect_oversold_bounce,
    detect_vwap_reversion,
    detect_range_bound_reversal,
    identify_mean_reversion_setup,
)


def test_detect_oversold_bounce_detects_valid_signal() -> None:
    """Test oversold bounce detection with valid signal."""
    # Price at 45.5 is very near lower band at 45 (%B = 5%)
    # close=45.5 > open=45.0 makes it bullish
    frame = pd.DataFrame({
        "open": [50.0, 49.0, 48.0, 47.0, 45.0],
        "high": [51.0, 50.0, 49.0, 48.0, 47.0],
        "low": [49.0, 48.0, 47.0, 46.0, 45.0],
        "close": [50.0, 49.0, 48.0, 47.0, 45.5],  # Bullish: 45.5 > 45.0
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1200.0],
        "volume_avg_5": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "bb_lower": [45.0, 45.0, 45.0, 45.0, 45.0],
        "bb_upper": [55.0, 55.0, 55.0, 55.0, 55.0],
        "rsi_14": [45.0, 40.0, 35.0, 30.0, 30.0],  # Oversold (must be < 35)
    })

    result = detect_oversold_bounce(frame)

    assert result is True


def test_detect_oversold_bounce_rejects_without_bullish_candle() -> None:
    """Test that bearish candle is rejected."""
    frame = pd.DataFrame({
        "open": [50.0, 49.0, 48.0, 47.0, 48.5],
        "high": [51.0, 50.0, 49.0, 48.0, 49.0],
        "low": [49.0, 48.0, 47.0, 46.0, 47.0],
        "close": [50.0, 49.0, 48.0, 47.0, 47.5],  # Bearish, close < open
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1200.0],
        "volume_avg_5": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "bb_lower": [45.0, 45.0, 45.0, 45.0, 45.0],
        "bb_upper": [55.0, 55.0, 55.0, 55.0, 55.0],
        "rsi_14": [45.0, 40.0, 35.0, 30.0, 32.0],
    })

    result = detect_oversold_bounce(frame)

    assert result is False


def test_detect_oversold_bounce_rejects_high_rsi() -> None:
    """Test that high RSI is rejected."""
    frame = pd.DataFrame({
        "open": [50.0, 49.0, 48.0, 47.0, 48.5],
        "high": [51.0, 50.0, 49.0, 48.0, 49.0],
        "low": [49.0, 48.0, 47.0, 46.0, 47.0],
        "close": [50.0, 49.0, 48.0, 47.0, 48.5],
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1200.0],
        "volume_avg_5": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "bb_lower": [45.0, 45.0, 45.0, 45.0, 45.0],
        "bb_upper": [55.0, 55.0, 55.0, 55.0, 55.0],
        "rsi_14": [45.0, 45.0, 45.0, 45.0, 45.0],  # Not oversold
    })

    result = detect_oversold_bounce(frame)

    assert result is False


def test_detect_vwap_reversion_detects_valid_signal() -> None:
    """Test VWAP reversion detection."""
    frame = pd.DataFrame({
        "open": [96.0, 95.0, 96.0],  # Current bar bullish: 97.5 > 96.0
        "high": [98.0, 97.0, 98.0],
        "low": [95.0, 94.0, 95.0],
        "close": [97.0, 96.0, 97.5],  # Bullish, close > open
        "volume": [1000.0, 1000.0, 1000.0],
        "volume_avg_5": [1000.0, 1000.0, 1000.0],
        "vwap": [100.0, 100.0, 100.0],  # Price 97.5 < 99.0 (1% below VWAP at 100)
    })

    result = detect_vwap_reversion(frame)

    assert result is True


def test_detect_vwap_reversion_rejects_above_vwap() -> None:
    """Test that price above VWAP is rejected."""
    frame = pd.DataFrame({
        "open": [100.0, 101.0, 102.0, 103.0, 102.5],
        "high": [101.0, 102.0, 103.0, 104.0, 103.0],
        "low": [99.0, 100.0, 101.0, 102.0, 101.0],
        "close": [100.0, 101.0, 102.0, 103.0, 102.5],
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "volume_avg_5": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "vwap": [100.0, 100.0, 100.0, 100.0, 100.0],  # Price above VWAP
    })

    result = detect_vwap_reversion(frame)

    assert result is False


def test_detect_range_bound_reversal_detects_valid_signal() -> None:
    """Test range-bound reversal detection."""
    # Create data with clear range and reversal at bottom
    # Range: 100-110, reversal at 101 (bottom 10%)
    highs = [110.0] * 8 + [108.0, 105.0, 104.0]  # Declining highs
    lows = [105.0] * 8 + [103.0, 101.0, 100.0]   # Declining lows, near bottom
    opens = [108.0] * 8 + [106.0, 104.0, 100.5]  # Previous bearish (104 < 106), current bullish (101.5 > 100.5)
    closes = [107.0] * 8 + [104.0, 101.0, 101.5]  # Previous bearish, current bullish

    frame = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * 10 + [1200.0],  # Volume spike on reversal
        "volume_avg_5": [1000.0] * 11,
        "rsi_14": [45.0] * 10 + [35.0],  # RSI not extreme
    })

    result = detect_range_bound_reversal(frame, lookback=10)

    assert result is True


def test_detect_range_bound_reversal_rejects_without_reversal() -> None:
    """Test that continued decline is rejected."""
    highs = [110.0] * 9 + [108.0]
    lows = [105.0] * 9 + [103.0]
    opens = [108.0] * 9 + [107.0]
    closes = [107.0] * 9 + [106.0]  # Continued bearish

    frame = pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * 10,
        "volume_avg_5": [1000.0] * 10,
        "rsi_14": [45.0] * 10,
    })

    result = detect_range_bound_reversal(frame, lookback=10)

    assert result is False


def test_identify_mean_reversion_setup_returns_oversold_first() -> None:
    """Test that oversold bounce takes priority."""
    # Price at 45.5 with bands at 45/55 gives %B = 5% (within 10%)
    frame = pd.DataFrame({
        "open": [50.0, 49.0, 48.0, 47.0, 45.0],
        "high": [51.0, 50.0, 49.0, 48.0, 47.0],
        "low": [49.0, 48.0, 47.0, 46.0, 45.0],
        "close": [50.0, 49.0, 48.0, 47.0, 45.5],  # Bullish: 45.5 > 45.0
        "volume": [1000.0, 1000.0, 1000.0, 1000.0, 1200.0],
        "volume_avg_5": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "bb_lower": [45.0, 45.0, 45.0, 45.0, 45.0],
        "bb_upper": [55.0, 55.0, 55.0, 55.0, 55.0],
        "rsi_14": [45.0, 40.0, 35.0, 30.0, 30.0],  # Must be < 35 to be oversold
        "vwap": [52.0, 52.0, 52.0, 52.0, 52.0],  # Also qualifies for VWAP
    })

    result = identify_mean_reversion_setup(frame)

    assert result == "oversold bounce"


def test_identify_mean_reversion_setup_returns_none_when_no_signal() -> None:
    """Test that None is returned when no signal present."""
    frame = pd.DataFrame({
        "open": [50.0, 51.0, 52.0, 53.0, 54.0],
        "high": [51.0, 52.0, 53.0, 54.0, 55.0],
        "low": [49.0, 50.0, 51.0, 52.0, 53.0],
        "close": [50.0, 51.0, 52.0, 53.0, 54.0],
        "volume": [1000.0] * 5,
        "volume_avg_5": [1000.0] * 5,
        "bb_lower": [45.0] * 5,
        "bb_upper": [55.0] * 5,
        "rsi_14": [55.0] * 5,  # Not oversold
        "vwap": [50.0] * 5,
    })

    result = identify_mean_reversion_setup(frame)

    assert result is None


def test_identify_mean_reversion_setup_returns_vwap_when_no_oversold() -> None:
    """Test VWAP signal when oversold not present."""
    frame = pd.DataFrame({
        "open": [96.0, 95.0, 96.0],
        "high": [98.0, 97.0, 98.0],
        "low": [95.0, 94.0, 95.0],
        "close": [97.0, 96.0, 97.5],  # Bullish
        "volume": [1000.0, 1000.0, 1000.0],
        "volume_avg_5": [1000.0, 1000.0, 1000.0],
        "bb_lower": [90.0, 90.0, 90.0],  # Not oversold (price not near lower band)
        "bb_upper": [110.0, 110.0, 110.0],
        "rsi_14": [45.0, 45.0, 45.0],  # Not oversold
        "vwap": [100.0, 100.0, 100.0],
    })

    result = identify_mean_reversion_setup(frame)

    assert result == "vwap reversion"
