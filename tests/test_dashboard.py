"""Tests for dashboard rendering with charts."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.runtime.dashboard import (
    _gauge_end_x,
    _gauge_end_y,
    _render_performance_section,
    _value_class,
    build_dashboard,
)


class TestDashboardCharts:
    """Tests for dashboard chart rendering."""

    def test_render_performance_section_with_data(self) -> None:
        """Test performance section rendering with trade data."""
        performance = {
            "total_trades": 20,
            "winning_trades": 12,
            "losing_trades": 8,
            "win_rate": 0.60,
            "avg_win": 150.0,
            "avg_loss": -75.0,
            "profit_factor": 2.0,
            "sharpe_ratio": 1.5,
            "largest_win": 500.0,
            "largest_loss": -200.0,
        }

        html = _render_performance_section(
            performance=performance,
            total_trades=20,
            wins=12,
            losses=8,
            win_rate=0.60,
            avg_win=150.0,
            avg_loss=-75.0,
            profit_factor=2.0,
            sharpe=1.5,
        )

        assert "Trade Distribution" in html
        assert "Win Rate Gauge" in html
        assert "Average Trade P&L" in html
        assert "60.0%" in html  # Win rate displayed
        assert "12 Wins" in html
        assert "8 Losses" in html

    def test_render_performance_section_no_trades(self) -> None:
        """Test performance section with no trades."""
        html = _render_performance_section(
            performance={},
            total_trades=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            profit_factor=0.0,
            sharpe=0.0,
        )

        assert "No trades yet" in html


class TestGaugeCalculations:
    """Tests for gauge chart calculations."""

    def test_gauge_end_x_50_percent(self) -> None:
        """Test gauge X coordinate at 50% win rate."""
        x = _gauge_end_x(0.5)
        assert 70 < x < 80  # Should be near center

    def test_gauge_end_x_0_percent(self) -> None:
        """Test gauge X coordinate at 0% win rate."""
        x = _gauge_end_x(0.0)
        assert x < 20  # Should be at left

    def test_gauge_end_x_100_percent(self) -> None:
        """Test gauge X coordinate at 100% win rate."""
        x = _gauge_end_x(1.0)
        assert x > 130  # Should be at right

    def test_gauge_end_y_consistency(self) -> None:
        """Test gauge Y coordinate is consistent."""
        y_0 = _gauge_end_y(0.0)
        y_50 = _gauge_end_y(0.5)
        y_100 = _gauge_end_y(1.0)

        # All Y values should be <= 70 (bottom of arc)
        assert y_0 <= 70
        assert y_50 <= 70
        assert y_100 <= 70


class TestValueClass:
    """Tests for value CSS class assignment."""

    def test_value_class_positive(self) -> None:
        """Test positive value class."""
        assert _value_class(1.5, 1.0) == "positive"
        assert _value_class(1.0, 1.0) == "positive"

    def test_value_class_negative(self) -> None:
        """Test negative value class."""
        assert _value_class(0.5, 1.0) == "negative"
        assert _value_class(0.0, 1.0) == "negative"


class TestBuildDashboard:
    """Tests for dashboard building."""

    def test_build_dashboard_creates_file(self, tmp_path: Path) -> None:
        """Test dashboard file creation."""
        from trading_bot.config.settings import Settings, AppSettings

        # Create minimal settings
        settings = Settings(
            app=AppSettings(
                state_db_path=str(tmp_path / "test.db"),
                log_dir=str(tmp_path / "logs"),
                dashboard_summary_path=str(tmp_path / "dashboard.json"),
                scan_results_path=str(tmp_path / "scan.json"),
                portfolio_summary_path=str(tmp_path / "portfolio.json"),
                backtest_summary_path=str(tmp_path / "backtest.json"),
            )
        )

        # Create dummy JSON files
        (tmp_path / "dashboard.json").write_text('{"summary": {}}')
        (tmp_path / "scan.json").write_text('{"candidates": []}')
        (tmp_path / "portfolio.json").write_text('{"summary": {}, "positions": []}')
        (tmp_path / "backtest.json").write_text('{"summary": {}}')

        output_path = tmp_path / "dashboard.html"
        result = build_dashboard(settings, output_path)

        assert result.exists()
        assert result.stat().st_size > 0
        content = result.read_text()
        assert "<!doctype html>" in content
        assert "Autonomous Trading Agent" in content
