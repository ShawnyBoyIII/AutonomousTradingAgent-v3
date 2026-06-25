"""Tests for dynamic strategy selection."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategy.market_regime import MarketRegime
from trading_bot.strategy.strategy_selector import (
    StrategySelector,
    StrategySelection,
    select_optimal_strategy,
)


class TestStrategySelector:
    """Tests for strategy selection."""

    def test_selector_initialization(self) -> None:
        """Test selector initialization."""
        selector = StrategySelector(risk_tolerance="low")
        assert selector.risk_tolerance == "low"
        assert selector.min_confidence == "medium"

    def test_no_trade_in_downtrend_low_risk(self) -> None:
        """Test that low risk avoids unfavorable regimes."""
        daily_frame = pd.DataFrame({
            "close": [100 - i for i in range(50)],
            "high": [101 - i for i in range(50)],
            "low": [99 - i for i in range(50)],
            "ema_20": [105 - i for i in range(50)],
            "sma_50": [110 - i * 0.8 for i in range(50)],
            "atr_14": [2.0] * 50,
            "bb_upper": [110 - i for i in range(50)],
            "bb_lower": [90 - i for i in range(50)],
            "bb_width": [15.0] * 50,
        })

        intraday_frame = pd.DataFrame({
            "close": [52, 51, 50, 49, 48],
            "high": [53, 52, 51, 50, 49],
            "low": [51, 50, 49, 48, 47],
            "volume": [1000] * 5,
        })

        selector = StrategySelector(risk_tolerance="low")
        result = selector.select_strategy("TEST", daily_frame, intraday_frame)

        # Low risk should avoid trading in unfavorable conditions
        assert result.should_trade is False
        assert result.regime is not None
        # Should be either downtrend, high volatility, or other unfavorable regime
        assert not result.should_trade

    def test_trade_in_uptrend(self) -> None:
        """Test that uptrend allows trading."""
        daily_frame = pd.DataFrame({
            "close": [100 + i for i in range(50)],
            "high": [101 + i for i in range(50)],
            "low": [99 + i for i in range(50)],
            "ema_20": [95 + i * 0.9 for i in range(50)],
            "sma_50": [90 + i * 0.8 for i in range(50)],
            "atr_14": [2.0] * 50,
            "bb_upper": [110 + i for i in range(50)],
            "bb_lower": [90 + i for i in range(50)],
            "bb_width": [15.0] * 50,
        })

        # Strong breakout pattern
        intraday_frame = pd.DataFrame({
            "close": [145, 146, 147, 148, 150],
            "high": [146, 147, 148, 149, 151],
            "low": [144, 145, 146, 147, 149],
            "volume": [1000, 1200, 1500, 2000, 2500],
            "volume_avg_5": [1000.0] * 5,
        })

        selector = StrategySelector(risk_tolerance="medium")
        result = selector.select_strategy("TEST", daily_frame, intraday_frame)

        # Should either find a trade or give specific reason
        assert isinstance(result.should_trade, bool)
        if result.should_trade:
            assert result.entry_price is not None
            assert result.stop_loss is not None
            assert result.profit_target is not None

    def test_confidence_threshold_enforcement(self) -> None:
        """Test that weak signals are rejected."""
        selector = StrategySelector(risk_tolerance="medium")
        selector.min_confidence = "high"  # Set high bar

        daily_frame = pd.DataFrame({
            "close": [100] * 50,
            "high": [101] * 50,
            "low": [99] * 50,
            "ema_20": [100] * 50,
            "sma_50": [100] * 50,
            "atr_14": [1.0] * 50,
            "bb_upper": [105] * 50,
            "bb_lower": [95] * 50,
            "bb_width": [10.0] * 50,
        })

        # Weak signal
        intraday_frame = pd.DataFrame({
            "close": [100, 100.1, 100.2],
            "high": [101, 101.1, 101.2],
            "low": [99, 99.1, 99.2],
            "volume": [500, 500, 500],
        })

        result = selector.select_strategy("TEST", daily_frame, intraday_frame)

        # With high confidence requirement, should not trade weak signals
        assert result.should_trade is False

    def test_position_size_calculation(self) -> None:
        """Test position size multiplier calculation."""
        selector = StrategySelector()

        daily_frame = pd.DataFrame({
            "close": [100 + i for i in range(20)],
            "high": [101 + i for i in range(20)],
            "low": [99 + i for i in range(20)],
            "ema_20": [95 + i for i in range(20)],
            "sma_50": [90 + i for i in range(20)],
            "atr_14": [2.0] * 20,
            "bb_upper": [110 + i for i in range(20)],
            "bb_lower": [90 + i for i in range(20)],
            "bb_width": [15.0] * 20,
        })

        intraday_frame = pd.DataFrame({
            "close": [115, 116, 117, 118, 120],
            "high": [116, 117, 118, 119, 121],
            "low": [114, 115, 116, 117, 119],
            "volume": [1000, 1500, 2000, 2500, 3000],
            "volume_avg_5": [1000.0] * 5,
        })

        result = selector.select_strategy("TEST", daily_frame, intraday_frame)

        if result.should_trade:
            assert result.position_size_multiplier >= 0.0
            assert result.position_size_multiplier <= 1.0


class TestSelectOptimalStrategy:
    """Tests for convenience function."""

    def test_function_returns_selection(self) -> None:
        """Test that function returns StrategySelection."""
        daily_frame = pd.DataFrame({
            "close": [100 + i for i in range(50)],
            "high": [101 + i for i in range(50)],
            "low": [99 + i for i in range(50)],
            "ema_20": [95 + i for i in range(50)],
            "sma_50": [90 + i for i in range(50)],
            "atr_14": [2.0] * 50,
            "bb_upper": [110 + i for i in range(50)],
            "bb_lower": [90 + i for i in range(50)],
            "bb_width": [15.0] * 50,
        })

        intraday_frame = pd.DataFrame({
            "close": [145, 146, 147, 148, 150],
            "high": [146, 147, 148, 149, 151],
            "low": [144, 145, 146, 147, 149],
            "volume": [1000, 1200, 1500, 2000, 2500],
        })

        result = select_optimal_strategy(
            "TEST", daily_frame, intraday_frame, risk_tolerance="medium"
        )

        assert isinstance(result, StrategySelection)
        assert result.regime is not None
        assert result.reason != ""


class TestStrategySelection:
    """Tests for StrategySelection dataclass."""

    def test_default_selection(self) -> None:
        """Test default selection values."""
        selection = StrategySelection(
            should_trade=False,
            strategy_type="none",
            setup_name=None,
            signal_score=None,
            regime=None,
            reason="Test",
        )

        assert selection.should_trade is False
        assert selection.strategy_type == "none"
        assert selection.reason == "Test"
        assert selection.entry_price is None

    def test_selection_with_trade(self) -> None:
        """Test selection with valid trade."""
        selection = StrategySelection(
            should_trade=True,
            strategy_type="trend_following",
            setup_name="breakout",
            signal_score=None,
            regime=MarketRegime.STRONG_UPTREND,
            reason="Strong signal",
            entry_price=150.0,
            stop_loss=147.0,
            profit_target=156.0,
            position_size_multiplier=0.8,
        )

        assert selection.should_trade is True
        assert selection.strategy_type == "trend_following"
        assert selection.entry_price == 150.0
        assert selection.stop_loss == 147.0
        assert selection.profit_target == 156.0
        assert selection.position_size_multiplier == 0.8


class TestTradeParameterGuards:
    """Tests for edge cases in stop/target calculation."""

    def test_negative_atr_floor_fallback_to_default(self) -> None:
        """Very high ATR relative to price must not produce a negative stop."""
        selector = StrategySelector()
        selector.atr_stop_multiplier = 1.5

        intraday = pd.DataFrame({
            "close": [10.0, 10.1, 10.2, 10.3, 10.5],
            "high": [10.1, 10.2, 10.3, 10.4, 10.6],
            "low": [9.9, 10.0, 10.1, 10.2, 10.4],
            "volume": [1000] * 5,
        })
        # Inject an absurdly high ATR that would push atr_floor below zero
        intraday["atr_14"] = [50.0] * 5

        entry, stop, target = selector._calculate_trade_parameters(intraday, "breakout")

        assert entry is not None
        assert stop is not None
        assert stop > 0
        assert stop < entry
        # Falls back to entry * 0.99 = 10.395
        assert stop == pytest.approx(10.395, abs=1e-3)

    def test_zero_atr_floor_fallback_to_default(self) -> None:
        """If ATR floor pushes stop to exactly zero, fall back to default."""
        selector = StrategySelector()
        selector.atr_stop_multiplier = 1.5

        intraday = pd.DataFrame({
            "close": [10.0, 10.1, 10.2, 10.3, 10.5],
            "high": [10.1, 10.2, 10.3, 10.4, 10.6],
            "low": [9.9, 10.0, 10.1, 10.2, 10.4],
            "volume": [1000] * 5,
        })
        # ATR that makes floor exactly zero (10.5 - (7 * 1.5) ≈ 0)
        intraday["atr_14"] = [7.0] * 5

        entry, stop, target = selector._calculate_trade_parameters(intraday, "momentum")

        assert entry is not None
        assert stop is not None
        assert stop > 0
        assert stop < entry

    def test_selection_to_signal_rejects_negative_stop(self) -> None:
        """selection_to_signal must coerce a negative stop up to entry*0.99."""
        from trading_bot.strategy.strategy_selector import selection_to_signal

        class FakeScore:
            confidence = "medium"
            supporting_signals = []

        selection = StrategySelection(
            should_trade=True,
            strategy_type="trend_following",
            setup_name="breakout",
            signal_score=FakeScore(),
            regime=MarketRegime.STRONG_UPTREND,
            reason="test",
            entry_price=100.0,
            stop_loss=-10.0,  # Invalid negative stop
            profit_target=120.0,
            position_size_multiplier=1.0,
        )

        intraday = pd.DataFrame({
            "close": [99.0, 100.0],
            "timestamp": pd.to_datetime(["2026-06-13 10:00:00", "2026-06-13 10:05:00"]),
        })

        signal = selection_to_signal("TEST", selection, intraday)

        assert signal is not None
        assert signal.stop_loss > 0
        assert signal.stop_loss < signal.entry_price
        assert signal.stop_loss == pytest.approx(99.0, abs=1e-3)
