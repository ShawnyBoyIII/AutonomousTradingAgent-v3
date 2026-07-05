"""Tests for portfolio correlation monitoring."""

from __future__ import annotations

import pytest
import pandas as pd

from trading_bot.models.portfolio import Position
from trading_bot.risk.correlation import (
    CorrelationResult,
    compute_pearson_correlation,
    compute_portfolio_correlation,
    compute_returns,
    format_correlation_report,
)
from trading_bot.models.portfolio import PortfolioState
from trading_bot.runtime.orchestrator import _correlation_context_for_candidate
from trading_bot.config.settings import Settings


class TestComputeReturns:
    """Tests for compute_returns."""

    def test_empty(self) -> None:
        assert compute_returns([]) == []

    def test_single_price(self) -> None:
        assert compute_returns([100.0]) == []

    def test_simple_returns(self) -> None:
        returns = compute_returns([100, 110, 121])
        assert len(returns) == 2
        assert returns[0] == pytest.approx(0.1, abs=0.001)  # 110/100 - 1
        assert returns[1] == pytest.approx(0.1, abs=0.001)  # 121/110 - 1

    def test_zero_price_skipped(self) -> None:
        returns = compute_returns([0, 100, 110])
        assert len(returns) == 1
        assert returns[0] == pytest.approx(0.1, abs=0.001)


class TestComputePearsonCorrelation:
    """Tests for compute_pearson_correlation."""

    def test_perfect_positive(self) -> None:
        x = [1, 2, 3, 4, 5]
        y = [2, 4, 6, 8, 10]
        corr = compute_pearson_correlation(x, y)
        assert corr == pytest.approx(1.0, abs=0.001)

    def test_perfect_negative(self) -> None:
        x = [1, 2, 3, 4, 5]
        y = [10, 8, 6, 4, 2]
        corr = compute_pearson_correlation(x, y)
        assert corr == pytest.approx(-1.0, abs=0.001)

    def test_no_correlation(self) -> None:
        # Orthogonal series: alternating vs paired pattern → correlation = 0
        x = [1, -1, 1, -1, 1, -1, 1, -1]
        y = [1, 1, -1, -1, 1, 1, -1, -1]
        corr = compute_pearson_correlation(x, y)
        assert abs(corr) < 0.1  # Truly uncorrelated

    def test_too_few_elements(self) -> None:
        assert compute_pearson_correlation([1], [2]) == 0.0

    def test_zero_variance(self) -> None:
        """If one series is constant, correlation is 0 (no std)."""
        corr = compute_pearson_correlation([5, 5, 5, 5], [1, 2, 3, 4])
        assert corr == 0.0


class TestComputePortfolioCorrelation:
    """Tests for compute_portfolio_correlation."""

    def _make_position(self, qty: int = 10) -> Position:
        return Position(
            ticker="TEST",
            quantity=qty,
            average_cost=100.0,
            stop_loss=95.0,
            profit_target=110.0,
        )

    def test_single_position_no_pairs(self) -> None:
        positions = {"AAPL": self._make_position()}
        price_history = {"AAPL": [100, 101, 102, 103]}
        result = compute_portfolio_correlation(positions, price_history)
        assert result.pair_count == 0
        assert result.warning is None

    def test_two_positions(self) -> None:
        positions = {
            "AAPL": self._make_position(),
            "MSFT": self._make_position(),
        }
        # Correlated prices
        price_history = {
            "AAPL": [100, 101, 102, 103, 104, 105],
            "MSFT": [200, 202, 204, 206, 208, 210],
        }
        result = compute_portfolio_correlation(positions, price_history)
        assert result.pair_count == 1
        assert result.max_correlation == pytest.approx(1.0, abs=0.01)

    def test_three_positions(self) -> None:
        positions = {
            "AAPL": self._make_position(),
            "MSFT": self._make_position(),
            "GOOG": self._make_position(),
        }
        price_history = {
            "AAPL": [100, 101, 102, 103, 104, 105],
            "MSFT": [200, 202, 204, 206, 208, 210],
            "GOOG": [150, 149, 148, 147, 146, 145],
        }
        result = compute_portfolio_correlation(positions, price_history)
        assert result.pair_count == 3

    def test_high_correlation_warning(self) -> None:
        positions = {
            "AAPL": self._make_position(),
            "MSFT": self._make_position(),
        }
        price_history = {
            "AAPL": [100, 101, 102, 103, 104, 105],
            "MSFT": [200, 202, 204, 206, 208, 210],
        }
        result = compute_portfolio_correlation(
            positions, price_history, max_avg_correlation=0.5
        )
        assert result.warning is not None
        assert "0.5" in result.warning

    def test_low_correlation_no_warning(self) -> None:
        positions = {
            "AAPL": self._make_position(),
            "MSFT": self._make_position(),
        }
        # Random-ish, low correlation
        price_history = {
            "AAPL": [100, 101, 100, 102, 101, 103, 102, 104, 103, 105],
            "MSFT": [200, 199, 201, 198, 202, 197, 203, 196, 204, 195],
        }
        result = compute_portfolio_correlation(
            positions, price_history, max_avg_correlation=0.6
        )
        assert result.warning is None

    def test_missing_price_history(self) -> None:
        """Ticker without price history should be skipped."""
        positions = {
            "AAPL": self._make_position(),
            "MSFT": self._make_position(),
        }
        price_history = {"AAPL": [100, 101, 102]}  # MSFT missing
        result = compute_portfolio_correlation(positions, price_history)
        assert result.pair_count == 0

    def test_zero_quantity_skipped(self) -> None:
        positions = {
            "AAPL": self._make_position(qty=0),
            "MSFT": self._make_position(),
        }
        price_history = {
            "AAPL": [100, 101, 102],
            "MSFT": [200, 202, 204],
        }
        result = compute_portfolio_correlation(positions, price_history)
        assert result.pair_count == 0


class TestFormatCorrelationReport:
    """Tests for format_correlation_report."""

    def test_empty_result(self) -> None:
        result = CorrelationResult(pair_count=0)
        report = format_correlation_report(result)
        assert "2+ open positions" in report

    def test_with_data(self) -> None:
        result = CorrelationResult(
            avg_correlation=0.75,
            max_correlation=0.92,
            max_pair=("AAPL", "MSFT"),
            pair_count=3,
            correlation_matrix=[
                {"ticker_a": "AAPL", "ticker_b": "MSFT", "correlation": 0.92},
                {"ticker_a": "AAPL", "ticker_b": "GOOG", "correlation": 0.5},
                {"ticker_a": "MSFT", "ticker_b": "GOOG", "correlation": 0.3},
            ],
            warning="High correlation detected",
        )
        report = format_correlation_report(result)
        assert "0.7500" in report
        assert "0.9200" in report
        assert "AAPL" in report
        assert "MSFT" in report
        assert "High correlation" in report

    def test_sorts_by_abs_correlation(self) -> None:
        result = CorrelationResult(
            avg_correlation=0.5,
            max_correlation=0.9,
            pair_count=2,
            correlation_matrix=[
                {"ticker_a": "A", "ticker_b": "B", "correlation": 0.1},
                {"ticker_a": "C", "ticker_b": "D", "correlation": 0.9},
            ],
        )
        report = format_correlation_report(result)
        # The 0.9 pair should appear before the 0.1 pair
        assert report.index("0.9000") < report.index("0.1000")


class TestOrchestratorCorrelationContext:
    def test_candidate_context_includes_candidate_symbol(self, monkeypatch) -> None:
        settings = Settings()
        state = PortfolioState(
            cash=10000.0,
            equity=10000.0,
            positions={
                "MSFT": Position(
                    ticker="MSFT",
                    quantity=10,
                    average_cost=100.0,
                )
            },
        )

        def fake_fetch_bars(symbol: str, period: str, interval: str, settings=None):
            if symbol == "AAPL":
                closes = [100, 101, 102, 103, 104, 105]
            else:
                closes = [200, 202, 204, 206, 208, 210]
            return pd.DataFrame({"close": closes})

        monkeypatch.setattr(
            "trading_bot.runtime.orchestrator.market_data.fetch_bars",
            fake_fetch_bars,
        )

        avg_corr, max_avg_corr = _correlation_context_for_candidate(
            "AAPL",
            state,
            settings,
            {},
        )

        assert avg_corr is not None
        assert avg_corr == pytest.approx(1.0, abs=0.01)
        assert max_avg_corr == settings.monitoring.max_avg_correlation
