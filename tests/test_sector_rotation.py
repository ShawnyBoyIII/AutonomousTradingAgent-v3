"""Tests for sector rotation analysis."""

from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.strategy.sector_rotation import (
    SECTOR_ETFS,
    SectorMetrics,
    SectorRotationAnalysis,
    analyze_sector_rotation,
    filter_symbols_by_sector_strength,
    get_best_sectors_for_trading,
    should_trade_symbol_in_sector,
)


class TestSectorRotation:
    """Tests for sector rotation detection."""

    def test_analyze_sector_rotation_basic(self) -> None:
        """Test basic sector rotation analysis."""
        # Create mock sector data
        sector_data = {
            "XLK": pd.DataFrame({
                "close": [100 + i * 2 for i in range(30)],  # Strong uptrend
                "volume": [1000000] * 30,
            }),
            "XLF": pd.DataFrame({
                "close": [100 + i * 0.5 for i in range(30)],  # Weak uptrend
                "volume": [1000000] * 30,
            }),
            "XLU": pd.DataFrame({
                "close": [100 - i * 0.3 for i in range(30)],  # Downtrend
                "volume": [1000000] * 30,
            }),
        }

        spy_data = pd.DataFrame({
            "close": [100 + i * 1 for i in range(30)],
        })

        analysis = analyze_sector_rotation(sector_data, spy_data)

        assert isinstance(analysis, SectorRotationAnalysis)
        assert len(analysis.sectors) == 3

        # XLK should be ranked highest (strong trend)
        xlk = next((s for s in analysis.sectors if s.symbol == "XLK"), None)
        assert xlk is not None
        assert xlk.rank == 1  # Should be #1

    def test_get_best_sectors(self) -> None:
        """Test getting top sectors."""
        analysis = SectorRotationAnalysis(
            sectors=[
                SectorMetrics(symbol="XLK", name="Tech", rank=1, momentum_score=10),
                SectorMetrics(symbol="XLF", name="Financials", rank=2, momentum_score=8),
                SectorMetrics(symbol="XLE", name="Energy", rank=3, momentum_score=6),
            ]
        )

        best = get_best_sectors_for_trading(analysis, top_n=2)

        assert len(best) == 2
        assert "XLK" in best
        assert "XLF" in best

    def test_should_trade_symbol_in_strong_sector(self) -> None:
        """Test trading decision for strong sector."""
        analysis = SectorRotationAnalysis(
            sectors=[
                SectorMetrics(symbol="XLK", name="Tech", rank=1, momentum_score=10),
            ]
        )

        should_trade, reason = should_trade_symbol_in_sector("AAPL", "XLK", analysis)

        assert should_trade is True
        assert "rank 1" in reason.lower()

    def test_should_not_trade_weak_sector(self) -> None:
        """Test trading decision for weak sector."""
        analysis = SectorRotationAnalysis(
            sectors=[
                SectorMetrics(symbol="XLU", name="Utilities", rank=8, momentum_score=-5),
            ]
        )

        should_trade, reason = should_trade_symbol_in_sector("NEE", "XLU", analysis)

        assert should_trade is False
        assert "rank 8" in reason.lower() or "momentum" in reason.lower()

    def test_filter_symbols_by_sector(self) -> None:
        """Test filtering symbols by sector strength."""
        symbols_with_sectors = {
            "AAPL": "XLK",  # Tech
            "MSFT": "XLK",  # Tech
            "JPM": "XLF",   # Financials
            "XOM": "XLE",   # Energy (weak)
        }

        analysis = SectorRotationAnalysis(
            sectors=[
                SectorMetrics(symbol="XLK", name="Tech", rank=1, momentum_score=10),
                SectorMetrics(symbol="XLF", name="Financials", rank=2, momentum_score=8),
                SectorMetrics(symbol="XLE", name="Energy", rank=9, momentum_score=-5),
            ]
        )

        filtered = filter_symbols_by_sector_strength(symbols_with_sectors, analysis, min_rank=5)

        assert "AAPL" in filtered
        assert "MSFT" in filtered
        assert "JPM" in filtered
        assert "XOM" not in filtered  # Rank 9 > 5

    def test_sector_metrics_creation(self) -> None:
        """Test SectorMetrics dataclass."""
        metrics = SectorMetrics(
            symbol="XLK",
            name="Technology",
            price_change_1d=1.5,
            price_change_5d=5.0,
            price_change_20d=15.0,
            relative_strength=5.0,
            momentum_score=85.0,
            rank=1,
        )

        assert metrics.symbol == "XLK"
        assert metrics.momentum_score == 85.0
        assert metrics.rank == 1


class TestSectorConstants:
    """Tests for sector constants."""

    def test_sector_etfs_defined(self) -> None:
        """Test that sector ETFs are defined."""
        assert "XLK" in SECTOR_ETFS
        assert "XLF" in SECTOR_ETFS
        assert "XLE" in SECTOR_ETFS

        assert SECTOR_ETFS["XLK"] == "Technology"
        assert SECTOR_ETFS["XLF"] == "Financials"
