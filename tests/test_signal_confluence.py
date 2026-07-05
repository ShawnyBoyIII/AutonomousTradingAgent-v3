"""Tests for signal confluence scoring."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategy.market_regime import MarketRegime, RegimeMetrics
from trading_bot.strategy.signal_confluence import (
    SignalScore,
    calculate_signal_confluence,
    _score_to_confidence,
)


class TestSignalConfluence:
    """Tests for signal confluence calculation."""

    def test_calculate_confluence_breakout(self) -> None:
        """Test confluence calculation for breakout setup."""
        daily_frame = pd.DataFrame({
            "close": [100 + i for i in range(50)],
            "high": [101 + i for i in range(50)],
            "low": [99 + i for i in range(50)],
            "ema_20": [95 + i for i in range(50)],
            "sma_50": [90 + i for i in range(50)],
            "atr_14": [2.0] * 50,
            "bb_upper": [110 + i for i in range(50)],
            "bb_lower": [90 + i for i in range(50)],
        })

        intraday_frame = pd.DataFrame({
            "close": [148, 149, 150, 151, 152],
            "high": [149, 150, 151, 152, 153],
            "low": [147, 148, 149, 150, 151],
            "volume": [1000, 1200, 1500, 2000, 2500],
            "rsi_14": [55, 58, 60, 62, 65],
        })

        regime = MarketRegime.STRONG_UPTREND
        regime_metrics = RegimeMetrics(adx=35.0, price_vs_sma50=10.0)

        score = calculate_signal_confluence(
            symbol="TEST",
            daily_frame=daily_frame,
            intraday_frame=intraday_frame,
            regime=regime,
            regime_metrics=regime_metrics,
            setup_type="breakout",
        )

        assert isinstance(score, SignalScore)
        assert score.total_score > 0
        assert score.technical_score >= 0
        assert score.volume_score >= 0
        assert score.trend_score >= 0

    def test_high_volume_boosts_score(self) -> None:
        """Test that high volume increases score."""
        base_frame = pd.DataFrame({
            "close": [148, 149, 150],
            "high": [149, 150, 151],
            "low": [147, 148, 149],
            "volume": [1000, 1000, 800],  # Below average
        })

        high_volume_frame = pd.DataFrame({
            "close": [148, 149, 150],
            "high": [149, 150, 151],
            "low": [147, 148, 149],
            "volume": [1000, 1000, 3000],  # 3x average
        })

        regime = MarketRegime.RANGE_BOUND
        regime_metrics = RegimeMetrics()

        score_low_vol = calculate_signal_confluence(
            "TEST", pd.DataFrame({"close": [100], "high": [101], "low": [99], "ema_20": [95], "sma_50": [90], "atr_14": [2], "bb_upper": [110], "bb_lower": [90]}),
            base_frame, regime, regime_metrics, "mean_reversion"
        )

        score_high_vol = calculate_signal_confluence(
            "TEST", pd.DataFrame({"close": [100], "high": [101], "low": [99], "ema_20": [95], "sma_50": [90], "atr_14": [2], "bb_upper": [110], "bb_lower": [90]}),
            high_volume_frame, regime, regime_metrics, "mean_reversion"
        )

        assert score_high_vol.volume_score >= score_low_vol.volume_score

    def test_regime_alignment_scoring(self) -> None:
        """Test regime-strategy alignment scoring."""
        frame = pd.DataFrame({
            "close": [100],
            "high": [101],
            "low": [99],
            "volume": [1000],
        })

        daily_frame = pd.DataFrame({
            "close": [100], "high": [101], "low": [99],
            "ema_20": [95], "sma_50": [90], "atr_14": [2],
            "bb_upper": [110], "bb_lower": [90],
        })

        regime_uptrend = MarketRegime.STRONG_UPTREND
        regime_range = MarketRegime.RANGE_BOUND
        regime_metrics = RegimeMetrics()

        score_trend_aligned = calculate_signal_confluence(
            "TEST", daily_frame, frame, regime_uptrend, regime_metrics, "breakout"
        )

        score_trend_misaligned = calculate_signal_confluence(
            "TEST", daily_frame, frame, regime_range, regime_metrics, "breakout"
        )

        assert score_trend_aligned.regime_alignment >= score_trend_misaligned.regime_alignment

    def test_mean_reversion_aliases_score_as_mean_reversion(self) -> None:
        frame = pd.DataFrame({
            "close": [100],
            "high": [101],
            "low": [99],
            "volume": [1000],
        })

        daily_frame = pd.DataFrame({
            "close": [100], "high": [101], "low": [99],
            "ema_20": [90], "sma_50": [95], "atr_14": [2],
            "bb_upper": [110], "bb_lower": [90],
        })

        score = calculate_signal_confluence(
            "TEST",
            daily_frame,
            frame,
            MarketRegime.RANGE_BOUND,
            RegimeMetrics(),
            "oversold_bounce",
        )

        assert score.regime_alignment == 2.0


class TestScoreToConfidence:
    """Tests for confidence level conversion."""

    def test_very_high_confidence(self) -> None:
        """Test very high threshold."""
        assert _score_to_confidence(9.0) == "very_high"
        assert _score_to_confidence(8.5) == "very_high"

    def test_high_confidence(self) -> None:
        """Test high threshold."""
        assert _score_to_confidence(8.0) == "high"
        assert _score_to_confidence(7.0) == "high"

    def test_medium_confidence(self) -> None:
        """Test medium threshold."""
        assert _score_to_confidence(6.0) == "medium"
        assert _score_to_confidence(5.5) == "medium"

    def test_low_confidence(self) -> None:
        """Test low threshold."""
        assert _score_to_confidence(5.0) == "low"
        assert _score_to_confidence(4.0) == "low"

    def test_no_confidence(self) -> None:
        """Test no confidence threshold."""
        assert _score_to_confidence(3.0) == "none"
        assert _score_to_confidence(0.0) == "none"


class TestSignalScoreStructure:
    """Tests for SignalScore dataclass."""

    def test_default_score(self) -> None:
        """Test default signal score values."""
        score = SignalScore()

        assert score.total_score == 0.0
        assert score.confidence == "none"
        assert score.technical_score == 0.0
        assert score.supporting_signals == []
        assert score.opposing_signals == []
        assert score.risk_factors == []

    def test_score_with_values(self) -> None:
        """Test signal score with custom values."""
        score = SignalScore(
            total_score=7.5,
            confidence="high",
            technical_score=1.5,
            volume_score=2.0,
            setup_type="breakout",
            supporting_signals=["Strong trend", "High volume"],
        )

        assert score.total_score == 7.5
        assert score.confidence == "high"
        assert len(score.supporting_signals) == 2
        assert score.setup_type == "breakout"

    def test_position_size_calculation(self) -> None:
        """Test position size recommendation."""
        score = SignalScore(
            total_score=8.0,
            confidence="high",
        )
        # Should calculate based on score components
        assert score.recommended_position_size_pct >= 0.0
        assert score.recommended_position_size_pct <= 1.0
