"""Tests for strategy daily_filter module (25 lines)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from trading_bot.strategy.daily_filter import is_bullish_daily_regime


class TestIsBullishDailyRegime:
    def test_bullish_regime(self):
        """Test bullish regime: close > ema20 > sma50."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0],
            "ema_20": [98.0, 98.5, 99.0, 99.5, 100.0],
            "sma_50": [95.0, 95.5, 96.0, 96.5, 97.0],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is True

    def test_not_bullish_close_below_ema(self):
        """Not bullish when close < ema20."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 98.0],
            "ema_20": [98.0, 98.5, 99.0, 99.5, 100.0],
            "sma_50": [95.0, 95.5, 96.0, 96.5, 97.0],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_not_bullish_ema_below_sma(self):
        """Not bullish when ema20 < sma50."""
        data = {
            "close": [100.0, 101.0, 102.0, 103.0, 104.0],
            "ema_20": [98.0, 98.5, 99.0, 99.5, 96.0],
            "sma_50": [95.0, 95.5, 96.0, 96.5, 97.0],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_empty_frame(self):
        """Empty frame returns False."""
        frame = pd.DataFrame(columns=["close", "ema_20", "sma_50"])
        assert is_bullish_daily_regime(frame) is False

    def test_missing_close_column(self):
        """Returns False when close column is missing."""
        data = {
            "ema_20": [98.0, 98.5],
            "sma_50": [95.0, 95.5],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_missing_ema_column(self):
        """Returns False when ema_20 column is missing."""
        data = {
            "close": [100.0, 101.0],
            "sma_50": [95.0, 95.5],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_missing_sma_column(self):
        """Returns False when sma_50 column is missing."""
        data = {
            "close": [100.0, 101.0],
            "ema_20": [98.0, 98.5],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_nan_close(self):
        """Returns False when close is NaN."""
        data = {
            "close": [100.0, float("nan")],
            "ema_20": [98.0, 98.5],
            "sma_50": [95.0, 95.5],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_nan_ema(self):
        """Returns False when ema_20 is NaN."""
        data = {
            "close": [100.0, 101.0],
            "ema_20": [98.0, float("nan")],
            "sma_50": [95.0, 95.5],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_nan_sma(self):
        """Returns False when sma_50 is NaN."""
        data = {
            "close": [100.0, 101.0],
            "ema_20": [98.0, 98.5],
            "sma_50": [95.0, float("nan")],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_inf_values(self):
        """pd.isna doesn't catch infinity, so inf passes through."""
        data = {
            "close": [100.0, float("inf")],
            "ema_20": [98.0, 98.5],
            "sma_50": [95.0, 95.5],
        }
        frame = pd.DataFrame(data)
        # pd.isna() doesn't catch inf, so inf > 98.5 > 95.5 is True
        assert is_bullish_daily_regime(frame) is True

    def test_single_row(self):
        """Single row frame works correctly."""
        data = {
            "close": [104.0],
            "ema_20": [100.0],
            "sma_50": [97.0],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is True

    def test_ema_equals_sma(self):
        """Not bullish when ema20 == sma50 (needs strict >)."""
        data = {
            "close": [100.0, 101.0, 102.0],
            "ema_20": [98.0, 99.0, 100.0],
            "sma_50": [95.0, 96.0, 100.0],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False

    def test_close_equals_ema(self):
        """Not bullish when close == ema20 (needs strict >)."""
        data = {
            "close": [100.0, 101.0, 102.0],
            "ema_20": [98.0, 99.0, 102.0],
            "sma_50": [95.0, 96.0, 97.0],
        }
        frame = pd.DataFrame(data)
        assert is_bullish_daily_regime(frame) is False
