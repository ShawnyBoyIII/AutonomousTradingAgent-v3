"""Tests for strategy setup_rules module (93 lines)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from trading_bot.strategy.setup_rules import (
    detect_intraday_breakout,
    detect_intraday_momentum_continuation,
    identify_intraday_setup,
)


class TestDetectIntradayBreakout:
    def test_basic_breakout(self):
        """Test basic breakout detection."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 106.0],
            "volume": [1000, 1100, 1200, 1300, 2000],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=4) is True

    def test_no_breakout_below_range(self):
        """No breakout when close is below range high."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 103.5],
            "high": [101.0, 102.0, 103.0, 104.0, 104.0],
            "volume": [1000, 1100, 1200, 1300, 2000],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=4) is False

    def test_no_breakout_low_volume(self):
        """No breakout when volume is below average."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 106.0],
            "volume": [1000, 1100, 1200, 1300, 800],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=4) is False

    def test_empty_frame(self):
        """Empty frame returns False."""
        frame = pd.DataFrame(columns=["close", "high", "volume", "volume_avg_5"])
        assert detect_intraday_breakout(frame) is False

    def test_missing_columns(self):
        """Missing required columns returns False."""
        data = {"close": [100.0, 101.0]}
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame) is False

    def test_lookback_too_large(self):
        """Returns False when lookback > frame length."""
        data = {
            "close": [100.0, 101.0],
            "high": [101.0, 102.0],
            "volume": [1000, 1100],
            "volume_avg_5": [1000.0, 1050.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=5) is False

    def test_lookback_zero(self):
        """Returns False when lookback <= 0."""
        data = {
            "close": [100.0, 101.0],
            "high": [101.0, 102.0],
            "volume": [1000, 1100],
            "volume_avg_5": [1000.0, 1050.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=0) is False
        assert detect_intraday_breakout(frame, lookback=-1) is False

    def test_nan_values(self):
        """NaN in prior high returns False."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 105.0],
            "high": [101.0, float("nan"), 103.0, 104.0, 106.0],
            "volume": [1000, 1100, 1200, 1300, 2000],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=4) is False

    def test_infinite_values(self):
        """Infinity in prior high returns False."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 105.0],
            "high": [101.0, float("inf"), 103.0, 104.0, 106.0],
            "volume": [1000, 1100, 1200, 1300, 2000],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=4) is False

    def test_custom_lookback(self):
        """Test with custom lookback period."""
        data = {
            "close": [100.0, 101.0, 102.0, 105.0],
            "high": [101.0, 102.0, 103.0, 106.0],
            "volume": [1000, 1100, 1200, 2000],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_breakout(frame, lookback=2) is True


class TestDetectIntradayMomentumContinuation:
    def test_basic_momentum(self):
        """Test basic momentum continuation detection."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.5],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "volume": [1000, 1100, 1200, 1300, 1400, 1500],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_momentum_continuation(frame) is True

    def test_no_momentum_declining(self):
        """No momentum when price is declining."""
        data = {
            "close": [105.0, 104.0, 103.0, 102.0, 101.0, 100.0],
            "high": [106.0, 105.0, 104.0, 103.0, 102.0, 101.0],
            "low": [104.0, 103.0, 102.0, 101.0, 100.0, 99.0],
            "volume": [1000, 1100, 1200, 1300, 1400, 1500],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_momentum_continuation(frame) is False

    def test_no_momentum_low_volume(self):
        """No momentum when volume is too low."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "volume": [1000, 1100, 1200, 1300, 1400, 500],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_momentum_continuation(frame) is False

    def test_empty_frame(self):
        """Empty frame returns False."""
        frame = pd.DataFrame(columns=["close", "high", "low", "volume", "volume_avg_5"])
        assert detect_intraday_momentum_continuation(frame) is False

    def test_too_few_rows(self):
        """Returns False when fewer than 5 rows."""
        data = {
            "close": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "volume": [1000, 1100, 1200],
            "volume_avg_5": [1000.0, 1050.0, 1100.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_momentum_continuation(frame) is False

    def test_missing_columns(self):
        """Missing required columns returns False."""
        data = {"close": [100.0, 101.0, 102.0, 103.0, 104.0]}
        frame = pd.DataFrame(data)
        assert detect_intraday_momentum_continuation(frame) is False

    def test_flat_candle(self):
        """Returns False when high == low (flat candle)."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 104.0],
            "high": [104.0, 105.0, 106.0, 107.0, 108.0, 104.0],
            "low": [104.0, 105.0, 106.0, 107.0, 108.0, 104.0],
            "volume": [1000, 1100, 1200, 1300, 1400, 1500],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_momentum_continuation(frame) is False

    def test_close_not_near_high(self):
        """Returns False when close is not near the high of the candle."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 104.1],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "volume": [1000, 1100, 1200, 1300, 1400, 1500],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        }
        frame = pd.DataFrame(data)
        # Close (104.1) is near low (104.0), not high (106.0)
        assert detect_intraday_momentum_continuation(frame) is False

    def test_close_below_recent_average(self):
        """Returns False when close is below recent average."""
        data = {
            "close": [105.0, 106.0, 107.0, 108.0, 109.0, 105.5],
            "high": [106.0, 107.0, 108.0, 109.0, 110.0, 107.0],
            "low": [104.0, 105.0, 106.0, 107.0, 108.0, 104.0],
            "volume": [1000, 1100, 1200, 1300, 1400, 1500],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        }
        frame = pd.DataFrame(data)
        assert detect_intraday_momentum_continuation(frame) is False


class TestIdentifyIntradaySetup:
    def test_breakout_detected(self):
        """Test breakout setup identification."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 105.0],
            "high": [101.0, 102.0, 103.0, 104.0, 106.0],
            "volume": [1000, 1100, 1200, 1300, 2000],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        result = identify_intraday_setup(frame)
        assert result == "intraday breakout"

    def test_momentum_detected(self):
        """Test momentum continuation setup identification."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.5],
            "high": [101.0, 102.0, 103.0, 104.0, 106.5, 106.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0, 104.0],
            "volume": [1000, 1100, 1200, 1300, 1400, 1500],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0, 1250.0],
        }
        frame = pd.DataFrame(data)
        # Prior high at index 4 (106.5) > latest close (105.5), so no breakout
        # But momentum conditions are met: close > prev, close near high, close > recent avg
        result = identify_intraday_setup(frame)
        assert result == "intraday momentum continuation"

    def test_no_setup(self):
        """Test when no setup is detected."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 102.5],
            "high": [101.0, 102.0, 103.0, 104.0, 103.5],
            "volume": [1000, 1100, 1200, 1300, 800],
            "volume_avg_5": [1000.0, 1050.0, 1100.0, 1150.0, 1200.0],
        }
        frame = pd.DataFrame(data)
        result = identify_intraday_setup(frame)
        assert result is None

    def test_empty_frame(self):
        """Test empty frame returns None."""
        frame = pd.DataFrame(columns=["close", "high", "volume", "volume_avg_5"])
        result = identify_intraday_setup(frame)
        assert result is None
