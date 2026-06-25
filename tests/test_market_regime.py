"""Tests for market regime detection."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategy.market_regime import (
    MarketRegime,
    RegimeMetrics,
    detect_market_regime,
    get_recommended_strategy,
    should_trade_regime,
)


class TestMarketRegimeDetection:
    """Tests for regime detection."""

    def test_detect_strong_uptrend(self) -> None:
        """Test detection of strong uptrend."""
        # Create uptrend data: ADX > 25, price well above MAs
        data = {
            "close": [100 + i * 2 for i in range(50)],  # Steady uptrend
            "high": [101 + i * 2 for i in range(50)],
            "low": [99 + i * 2 for i in range(50)],
            "ema_20": [90 + i * 2 for i in range(50)],
            "sma_50": [80 + i * 2.2 for i in range(50)],  # Lagging
            "atr_14": [1.0] * 50,
            "bb_upper": [110 + i * 2 for i in range(50)],
            "bb_middle": [100 + i * 2 for i in range(50)],
            "bb_lower": [90 + i * 2 for i in range(50)],
            "bb_width": [10.0] * 50,
        }
        frame = pd.DataFrame(data)

        regime, metrics = detect_market_regime(frame)

        assert isinstance(regime, MarketRegime)
        assert metrics.adx > 0
        assert metrics.price_vs_ema20 > 0
        assert metrics.price_vs_sma50 > 0

    def test_detect_range_bound(self) -> None:
        """Test detection of range-bound market."""
        # Create sideways data
        data = {
            "close": [100 + (i % 10 - 5) * 0.5 for i in range(50)],  # Oscillating
            "high": [102 + (i % 10 - 5) * 0.5 for i in range(50)],
            "low": [98 + (i % 10 - 5) * 0.5 for i in range(50)],
            "ema_20": [100.0] * 50,
            "sma_50": [100.0] * 50,
            "atr_14": [0.5] * 50,
            "bb_upper": [105.0] * 50,
            "bb_middle": [100.0] * 50,
            "bb_lower": [95.0] * 50,
            "bb_width": [10.0] * 50,
        }
        frame = pd.DataFrame(data)

        regime, metrics = detect_market_regime(frame)

        # In ranging market, ADX should be low
        assert metrics.adx < 25 or regime == MarketRegime.RANGE_BOUND

    def test_insufficient_data(self) -> None:
        """Test handling of insufficient data."""
        frame = pd.DataFrame({
            "close": [100, 101],
            "high": [102, 103],
            "low": [99, 100],
        })

        regime, metrics = detect_market_regime(frame)

        assert regime == MarketRegime.RANGE_BOUND
        assert metrics.adx == 0.0

    def test_missing_columns(self) -> None:
        """Test handling of missing required columns."""
        frame = pd.DataFrame({"close": [100] * 60})

        regime, metrics = detect_market_regime(frame)

        assert regime == MarketRegime.RANGE_BOUND


class TestRegimeRecommendations:
    """Tests for regime-based recommendations."""

    def test_recommendation_uptrend(self) -> None:
        """Test strategy recommendation for uptrend."""
        assert get_recommended_strategy(MarketRegime.STRONG_UPTREND) == "trend_following"
        assert get_recommended_strategy(MarketRegime.WEAK_UPTREND) == "trend_following"

    def test_recommendation_range(self) -> None:
        """Test strategy recommendation for range-bound."""
        assert get_recommended_strategy(MarketRegime.RANGE_BOUND) == "mean_reversion"

    def test_recommendation_downtrend(self) -> None:
        """Test strategy recommendation for downtrend."""
        assert get_recommended_strategy(MarketRegime.STRONG_DOWNTREND) == "none"
        assert get_recommended_strategy(MarketRegime.WEAK_DOWNTREND) == "mean_reversion"


class TestShouldTradeRegime:
    """Tests for regime trading decisions."""

    def test_low_risk_tolerance(self) -> None:
        """Test low risk tolerance filters."""
        assert should_trade_regime(MarketRegime.STRONG_UPTREND, "low") is True
        assert should_trade_regime(MarketRegime.RANGE_BOUND, "low") is True
        assert should_trade_regime(MarketRegime.WEAK_UPTREND, "low") is False
        assert should_trade_regime(MarketRegime.STRONG_DOWNTREND, "low") is False

    def test_medium_risk_tolerance(self) -> None:
        """Test medium risk tolerance filters."""
        assert should_trade_regime(MarketRegime.STRONG_UPTREND, "medium") is True
        assert should_trade_regime(MarketRegime.WEAK_UPTREND, "medium") is True
        assert should_trade_regime(MarketRegime.RANGE_BOUND, "medium") is True
        assert should_trade_regime(MarketRegime.WEAK_DOWNTREND, "medium") is True
        assert should_trade_regime(MarketRegime.STRONG_DOWNTREND, "medium") is False
        assert should_trade_regime(MarketRegime.HIGH_VOLATILITY, "medium") is False

    def test_high_risk_tolerance(self) -> None:
        """Test high risk tolerance allows all."""
        assert should_trade_regime(MarketRegime.STRONG_DOWNTREND, "high") is True
        assert should_trade_regime(MarketRegime.HIGH_VOLATILITY, "high") is True


class TestRegimeMetrics:
    """Tests for RegimeMetrics dataclass."""

    def test_metrics_defaults(self) -> None:
        """Test default metric values."""
        metrics = RegimeMetrics()

        assert metrics.trend_strength == 0.0
        assert metrics.volatility_percentile == 0.0
        assert metrics.adx == 0.0
        assert metrics.bb_squeeze is False

    def test_metrics_with_values(self) -> None:
        """Test metrics with custom values."""
        metrics = RegimeMetrics(
            trend_strength=0.8,
            volatility_percentile=0.3,
            adx=35.0,
            price_vs_ema20=5.0,
            bb_squeeze=True,
        )

        assert metrics.trend_strength == 0.8
        assert metrics.adx == 35.0
        assert metrics.bb_squeeze is True
