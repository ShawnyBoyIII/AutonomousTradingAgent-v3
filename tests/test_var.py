"""Tests for Value at Risk (VaR) and stress testing."""

from __future__ import annotations

import pytest

from trading_bot.models.portfolio import Position
from trading_bot.risk.var import (
    STRESS_SCENARIOS,
    StressResult,
    VaRResult,
    compute_historical_var,
    compute_parametric_var,
    compute_stress_test,
    format_stress_report,
    format_var_report,
)


def _make_position(ticker: str, qty: int = 10, cost: float = 100.0) -> Position:
    return Position(
        ticker=ticker,
        quantity=qty,
        average_cost=cost,
        stop_loss=cost * 0.95,
        profit_target=cost * 1.10,
    )


def _make_price_history(start: float, count: int, trend: float = 0.0) -> list[float]:
    """Generate a simple price series with optional trend."""
    prices = []
    current = start
    for i in range(count):
        prices.append(round(current, 2))
        current = current * (1.0 + trend) + (i % 3 - 1) * 0.5
        if current <= 0:
            current = start
    return prices


class TestHistoricalVaR:
    """Tests for compute_historical_var."""

    def test_no_positions(self) -> None:
        result = compute_historical_var({}, {}, {}, confidence=0.95)
        assert result.var_dollar == 0.0

    def test_single_position(self) -> None:
        positions = {"AAPL": _make_position("AAPL", qty=100, cost=100.0)}
        position_values = {"AAPL": 10000.0}
        price_history = {"AAPL": _make_price_history(100, 60)}

        result = compute_historical_var(
            position_values, price_history, positions, confidence=0.95
        )
        assert result.method == "historical"
        assert result.confidence == 0.95
        assert result.portfolio_value == 10000.0
        assert result.var_dollar > 0
        assert result.var_pct > 0

    def test_multiple_positions(self) -> None:
        positions = {
            "AAPL": _make_position("AAPL", qty=100),
            "MSFT": _make_position("MSFT", qty=50),
        }
        position_values = {"AAPL": 15000.0, "MSFT": 10000.0}
        price_history = {
            "AAPL": _make_price_history(150, 60),
            "MSFT": _make_price_history(200, 60),
        }
        result = compute_historical_var(
            position_values, price_history, positions, confidence=0.95
        )
        assert result.var_dollar > 0
        assert result.expected_shortfall_dollar >= result.var_dollar

    def test_insufficient_history(self) -> None:
        positions = {"AAPL": _make_position("AAPL")}
        position_values = {"AAPL": 1000.0}
        price_history = {"AAPL": [100]}  # Only 1 price

        result = compute_historical_var(
            position_values, price_history, positions
        )
        assert "insufficient" in result.detail

    def test_different_confidence_levels(self) -> None:
        """Higher confidence → higher VaR."""
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        price_history = {"AAPL": _make_price_history(100, 60)}

        var_95 = compute_historical_var(
            position_values, price_history, positions, confidence=0.95
        )
        var_99 = compute_historical_var(
            position_values, price_history, positions, confidence=0.99
        )
        assert var_99.var_dollar >= var_95.var_dollar

    def test_expected_shortfall_exceeds_var(self) -> None:
        """Expected shortfall should be >= VaR (it's the average of tail losses)."""
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        price_history = {"AAPL": _make_price_history(100, 60)}

        result = compute_historical_var(
            position_values, price_history, positions, confidence=0.95
        )
        assert result.expected_shortfall_dollar >= result.var_dollar


class TestParametricVaR:
    """Tests for compute_parametric_var."""

    def test_no_positions(self) -> None:
        result = compute_parametric_var({}, {}, {})
        assert result.var_dollar == 0.0

    def test_single_position(self) -> None:
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        price_history = {"AAPL": _make_price_history(100, 60)}

        result = compute_parametric_var(
            position_values, price_history, positions, confidence=0.95
        )
        assert result.method == "parametric"
        assert result.var_dollar > 0
        assert result.var_pct > 0

    def test_parametric_vs_historical(self) -> None:
        """Parametric VaR should be in same ballpark as historical."""
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        price_history = {"AAPL": _make_price_history(100, 60)}

        var_h = compute_historical_var(
            position_values, price_history, positions, confidence=0.95
        )
        var_p = compute_parametric_var(
            position_values, price_history, positions, confidence=0.95
        )
        # Both should be positive and within an order of magnitude
        assert var_h.var_dollar > 0
        assert var_p.var_dollar > 0
        assert var_p.var_dollar < var_h.var_dollar * 10
        assert var_p.var_dollar > var_h.var_dollar / 10


class TestStressTest:
    """Tests for compute_stress_test."""

    def test_no_positions(self) -> None:
        result = compute_stress_test({}, {})
        assert result == []

    def test_single_position(self) -> None:
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        results = compute_stress_test(position_values, positions)

        assert len(results) == len(STRESS_SCENARIOS)
        for result in results:
            assert result.portfolio_loss < 0  # Losses are negative
            assert result.portfolio_loss_pct > 0
            assert result.portfolio_loss <= 0  # All scenarios are losses

    def test_market_crash_largest_loss(self) -> None:
        """Market crash (2008) should have the largest loss."""
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        results = compute_stress_test(position_values, positions)

        crash_result = next(r for r in results if r.scenario == "market_crash_2008")
        mild_result = next(r for r in results if r.scenario == "mild_correction")

        assert crash_result.portfolio_loss < mild_result.portfolio_loss
        assert crash_result.portfolio_loss_pct > mild_result.portfolio_loss_pct

    def test_custom_scenario(self) -> None:
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        custom = {"test_crash": -0.20}

        results = compute_stress_test(position_values, positions, scenarios=custom)
        assert len(results) == 1
        assert results[0].scenario == "test_crash"
        assert results[0].portfolio_loss == pytest.approx(-2000.0, abs=0.01)
        assert results[0].portfolio_loss_pct == pytest.approx(20.0, abs=0.01)

    def test_per_position_breakdown(self) -> None:
        positions = {
            "AAPL": _make_position("AAPL", qty=100, cost=100.0),
            "MSFT": _make_position("MSFT", qty=50, cost=200.0),
        }
        position_values = {"AAPL": 10000.0, "MSFT": 10000.0}
        results = compute_stress_test(position_values, positions)

        for result in results:
            assert "AAPL" in result.per_position
            assert "MSFT" in result.per_position

    def test_sorted_by_severity(self) -> None:
        """Results should be sorted by loss (most negative first)."""
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        results = compute_stress_test(position_values, positions)

        losses = [r.portfolio_loss for r in results]
        assert losses == sorted(losses)

    def test_loss_pct_proportional_to_shock(self) -> None:
        """Loss % should be proportional to shock %."""
        positions = {"AAPL": _make_position("AAPL", qty=100)}
        position_values = {"AAPL": 10000.0}
        custom = {"a": -0.10, "b": -0.05}
        results = compute_stress_test(position_values, positions, scenarios=custom)

        assert results[0].portfolio_loss_pct == pytest.approx(10.0, abs=0.1)
        assert results[1].portfolio_loss_pct == pytest.approx(5.0, abs=0.1)


class TestFormatReports:
    """Tests for format_var_report and format_stress_report."""

    def test_format_var_empty(self) -> None:
        result = VaRResult(method="historical", confidence=0.95)
        report = format_var_report(result)
        assert "insufficient" in report.lower() or "no position" in report.lower()

    def test_format_var_with_data(self) -> None:
        result = VaRResult(
            method="historical",
            confidence=0.95,
            var_dollar=500.0,
            var_pct=5.0,
            portfolio_value=10000.0,
            expected_shortfall_dollar=750.0,
        )
        report = format_var_report(result)
        assert "Historical" in report
        assert "500" in report
        assert "5.00%" in report
        assert "750" in report
        assert "95%" in report

    def test_format_var_parametric(self) -> None:
        result = VaRResult(
            method="parametric",
            confidence=0.99,
            var_dollar=800.0,
            var_pct=8.0,
            portfolio_value=10000.0,
        )
        report = format_var_report(result)
        assert "Parametric" in report
        assert "99%" in report

    def test_format_stress_empty(self) -> None:
        report = format_stress_report([])
        assert "No positions" in report

    def test_format_stress_with_data(self) -> None:
        results = [
            StressResult(
                scenario="market_crash",
                portfolio_loss=-3500.0,
                portfolio_loss_pct=35.0,
            ),
        ]
        report = format_stress_report(results)
        assert "market_crash" in report
        assert "3,500" in report  # formatted with comma
        assert "35.00%" in report
