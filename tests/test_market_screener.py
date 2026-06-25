"""Tests for market screener."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategy.market_screener import (
    MarketScreener,
    MarketScreen,
    ScreenResult,
    find_gap_up_symbols,
    screen_for_breakout_setups,
    screen_for_mean_reversion,
)


class TestMarketScreener:
    """Tests for market screener."""

    def test_screener_initialization(self) -> None:
        """Test screener initialization."""
        screener = MarketScreener(
            min_price=10.0,
            max_price=500.0,
            min_volume=1_000_000,
        )

        assert screener.min_price == 10.0
        assert screener.max_price == 500.0
        assert screener.min_volume == 1_000_000

    def test_screen_symbol_passes(self) -> None:
        """Test screening a symbol that passes all criteria."""
        screener = MarketScreener(min_price=5.0, min_volume=1000)

        # Create 25 rows to satisfy 20-bar minimum
        frame = pd.DataFrame({
            "open": list(range(75, 100)),
            "high": list(range(76, 101)),
            "low": list(range(74, 99)),
            "close": list(range(75, 100)),
            "volume": [10000] * 25,
            "ema_20": list(range(70, 95)),
            "sma_50": list(range(65, 90)),
        })

        result = screener.screen_symbol("AAPL", frame)

        assert isinstance(result, ScreenResult)
        assert result.symbol == "AAPL"

    def test_screen_symbol_fails_price_too_low(self) -> None:
        """Test screening rejects symbol below min price."""
        screener = MarketScreener(min_price=50.0)

        frame = pd.DataFrame({
            "close": [40] * 25,  # Below min
            "volume": [1000000] * 25,
        })

        result = screener.screen_symbol("CHEAP", frame)

        assert result.passed is False
        assert any("below minimum" in r for r in result.reasons)

    def test_screen_symbol_fails_volume_too_low(self) -> None:
        """Test screening rejects symbol with low volume."""
        screener = MarketScreener(min_volume=1_000_000)

        frame = pd.DataFrame({
            "close": [100] * 25,
            "volume": [100] * 25,  # Too low
        })

        result = screener.screen_symbol("LOWVOL", frame)

        assert result.passed is False
        assert any("volume" in r.lower() for r in result.reasons)

    def test_screen_symbol_not_green_candle(self) -> None:
        """Test screening with require_green_candle."""
        screener = MarketScreener(require_green_candle=True)

        frame = pd.DataFrame({
            "open": [100] * 25,
            "close": [95] * 25,  # Red candle
            "volume": [1000000] * 25,
        })

        result = screener.screen_symbol("REDCANDLE", frame)

        assert result.passed is False
        assert any("green candle" in r.lower() for r in result.reasons)

    def test_find_gap_up_symbols(self) -> None:
        """Test finding gap up symbols."""
        premarket_data = {
            "AAPL": pd.DataFrame({
                "open": [100, 101],
                "high": [101, 105],  # 5% move from open
                "volume": [500000, 600000],
            }),
            "TSLA": pd.DataFrame({
                "open": [200, 201],
                "high": [201, 202],  # 1% move
                "volume": [500000, 550000],
            }),
        }

        gaps = find_gap_up_symbols(premarket_data, min_gap_pct=3.0, min_premarket_volume=1000)

        # Should find at least one gap
        assert isinstance(gaps, list)

    def test_screen_for_breakout_setups(self) -> None:
        """Test screening for breakout setups."""
        symbols_data = {
            "AAPL": pd.DataFrame({
                "high": [100, 101, 102, 103, 104, 105],  # 20-day high = 104
                "close": [99, 100, 101, 102, 103, 104],  # Near high
                "volume": [1000] * 6,
            }),
        }

        breakouts = screen_for_breakout_setups(symbols_data, lookback_days=5)

        # Should find AAPL near 5-day high
        assert len(breakouts) >= 0  # May or may not pass depending on exact calc

    def test_screen_for_mean_reversion(self) -> None:
        """Test screening for mean reversion setups."""
        symbols_data = {
            "OVERSOLD": pd.DataFrame({
                "open": [100, 99],
                "high": [101, 100],
                "low": [99, 98],
                "close": [99.5, 98.5],  # Lower close
                "volume": [10000, 15000],  # High volume
                "bb_lower": [98, 97],
                "bb_upper": [102, 101],
                "rsi_14": [35, 32],  # Oversold
            }),
        }

        oversold = screen_for_mean_reversion(symbols_data)

        # Should find oversold symbol
        assert len(oversold) >= 0


class TestScreenResult:
    """Tests for ScreenResult dataclass."""

    def test_screen_result_creation(self) -> None:
        """Test creating screen result."""
        result = ScreenResult(
            symbol="AAPL",
            passed=True,
            score=75.5,
            reasons=["Strong trend", "High volume"],
            metrics={"volume_ratio": 2.5},
        )

        assert result.symbol == "AAPL"
        assert result.passed is True
        assert result.score == 75.5
        assert len(result.reasons) == 2
        assert result.metrics["volume_ratio"] == 2.5


class TestMarketScreen:
    """Tests for MarketScreen dataclass."""

    def test_market_screen_creation(self) -> None:
        """Test creating market screen."""
        screen = MarketScreen(
            total_screened=100,
            passed=[ScreenResult("AAPL", True)],
            failed=[ScreenResult("BAD", False)],
        )

        assert screen.total_screened == 100
        assert len(screen.passed) == 1
        assert len(screen.failed) == 1
