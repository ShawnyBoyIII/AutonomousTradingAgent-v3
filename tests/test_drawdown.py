"""Tests for drawdown monitoring."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from trading_bot.models.portfolio import PortfolioState
from trading_bot.monitoring.drawdown import (
    DrawdownMetrics,
    compute_drawdown,
    compute_drawdown_from_ledger,
    compute_session_drawdown,
    format_drawdown_report,
)
from trading_bot.portfolio.ledger import PortfolioLedger


class TestComputeDrawdown:
    """Tests for compute_drawdown pure function."""

    def test_empty_series(self) -> None:
        metrics = compute_drawdown([])
        assert metrics.max_drawdown_pct == 0.0
        assert metrics.current_drawdown_pct == 0.0

    def test_single_value(self) -> None:
        metrics = compute_drawdown([10000.0])
        assert metrics.max_drawdown_pct == 0.0
        assert metrics.current_drawdown_pct == 0.0

    def test_no_drawdown(self) -> None:
        """Monotonically increasing equity — no drawdown."""
        metrics = compute_drawdown([10000, 10500, 11000, 12000])
        assert metrics.max_drawdown_pct == 0.0
        assert metrics.current_drawdown_pct == 0.0

    def test_simple_drawdown(self) -> None:
        """Peak at 12000, drops to 9000 = 25% drawdown."""
        metrics = compute_drawdown([10000, 12000, 9000, 9500])
        assert metrics.max_drawdown_pct == pytest.approx(25.0, abs=0.01)
        assert metrics.peak_equity == 12000.0
        assert metrics.trough_equity == 9000.0

    def test_current_drawdown(self) -> None:
        """Current equity below peak = current drawdown."""
        metrics = compute_drawdown([10000, 12000, 11000])
        assert metrics.current_drawdown_pct == pytest.approx(
            (12000 - 11000) / 12000 * 100, abs=0.01
        )

    def test_recovered_drawdown(self) -> None:
        """Dipped then recovered — current DD should be 0."""
        metrics = compute_drawdown([10000, 8000, 12000])
        assert metrics.current_drawdown_pct == 0.0
        assert metrics.max_drawdown_pct == pytest.approx(20.0, abs=0.01)

    def test_multiple_drawdowns_tracks_max(self) -> None:
        """Two drawdowns: 10% then 20%. Max should be 20%."""
        metrics = compute_drawdown([10000, 9000, 11000, 8800])
        assert metrics.max_drawdown_pct == pytest.approx(20.0, abs=0.1)

    def test_underwater_bars(self) -> None:
        """Count consecutive bars below peak."""
        metrics = compute_drawdown([10000, 9500, 9000, 8500, 11000])
        assert metrics.max_underwater_bars == 3

    def test_underwater_bars_reset_on_recovery(self) -> None:
        metrics = compute_drawdown([10000, 9500, 10000, 9000, 9500])
        assert metrics.max_underwater_bars == 2


class TestComputeDrawdownFromLedger:
    """Tests for compute_drawdown_from_ledger."""

    def test_no_history(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "ledger.db")
        ledger.initialize()
        metrics = compute_drawdown_from_ledger(ledger)
        assert metrics.max_drawdown_pct == 0.0

    def test_with_history(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "ledger.db")
        ledger.initialize()

        # Record equity snapshots: 10000 → 12000 → 9000 → 9500
        for equity in [10000, 12000, 9000, 9500]:
            state = PortfolioState(cash=equity, equity=float(equity))
            ledger.record_equity_snapshot(state)

        metrics = compute_drawdown_from_ledger(ledger)
        assert metrics.max_drawdown_pct == pytest.approx(25.0, abs=0.01)
        assert metrics.peak_equity == 12000.0
        assert metrics.recovery_equity == 9500.0

    def test_calmar_ratio_positive_return(self, tmp_path: Path) -> None:
        """Calmar ratio should be positive when returns are positive."""
        ledger = PortfolioLedger(tmp_path / "ledger.db")
        ledger.initialize()

        # Grow from 10000 to 15000 with a small drawdown
        for equity in [10000, 11000, 9000, 15000]:
            state = PortfolioState(cash=equity, equity=float(equity))
            ledger.record_equity_snapshot(state)

        metrics = compute_drawdown_from_ledger(ledger)
        assert metrics.calmar_ratio > 0

    def test_calmar_ratio_negative_return(self, tmp_path: Path) -> None:
        """Calmar ratio should be <= 0 when returns are negative."""
        ledger = PortfolioLedger(tmp_path / "ledger.db")
        ledger.initialize()

        for equity in [10000, 9000, 11000, 8000]:
            state = PortfolioState(cash=equity, equity=float(equity))
            ledger.record_equity_snapshot(state)

        metrics = compute_drawdown_from_ledger(ledger)
        assert metrics.calmar_ratio <= 0


class TestComputeSessionDrawdown:
    """Tests for compute_session_drawdown."""

    def test_empty_history(self) -> None:
        high, low, dd = compute_session_drawdown([], 10000)
        assert high == 10000
        assert low == 10000
        assert dd == 0.0

    def test_no_drawdown(self) -> None:
        high, low, dd = compute_session_drawdown([10000, 11000, 12000], 13000)
        assert high == 13000
        assert low == 10000
        assert dd == 0.0

    def test_with_drawdown(self) -> None:
        high, low, dd = compute_session_drawdown([10000, 12000, 9000], 9500)
        assert high == 12000
        assert low == 9000
        assert dd == pytest.approx(25.0, abs=0.01)


class TestFormatDrawdownReport:
    """Tests for format_drawdown_report."""

    def test_empty_metrics(self) -> None:
        metrics = DrawdownMetrics()
        report = format_drawdown_report(metrics)
        assert "No equity history" in report

    def test_with_data(self) -> None:
        metrics = DrawdownMetrics(
            current_drawdown_pct=5.0,
            max_drawdown_pct=10.0,
            peak_equity=12000.0,
            trough_equity=10800.0,
            recovery_equity=11400.0,
            calmar_ratio=2.5,
        )
        report = format_drawdown_report(metrics)
        assert "Current Drawdown" in report
        assert "Max Drawdown" in report
        assert "Calmar Ratio" in report
        assert "12,000" in report  # formatted with comma


class TestEquityHistoryTable:
    """Tests for the equity_history table via PortfolioLedger."""

    def test_record_and_list_equity(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "ledger.db")
        ledger.initialize()

        ts1 = datetime(2026, 6, 20, 10, 0, 0)
        ts2 = datetime(2026, 6, 20, 11, 0, 0)
        state1 = PortfolioState(cash=10000, equity=10000)
        state2 = PortfolioState(cash=9500, equity=10500)

        ledger.record_equity_snapshot(state1, timestamp=ts1)
        ledger.record_equity_snapshot(state2, timestamp=ts2)

        rows = ledger.list_equity_history()
        assert len(rows) == 2
        assert rows[0]["equity"] == 10000
        assert rows[1]["equity"] == 10500
        assert rows[0]["cash"] == 10000
        assert rows[1]["cash"] == 9500

    def test_list_equity_history_limit(self, tmp_path: Path) -> None:
        ledger = PortfolioLedger(tmp_path / "ledger.db")
        ledger.initialize()

        for i in range(10):
            state = PortfolioState(cash=float(i), equity=float(i + 1))
            ledger.record_equity_snapshot(state)

        rows = ledger.list_equity_history(limit=5)
        assert len(rows) == 5
        # Should return oldest 5 (ASC order)
        assert rows[0]["equity"] == 1.0
        assert rows[-1]["equity"] == 5.0

    def test_equity_history_persists_across_sessions(self, tmp_path: Path) -> None:
        """Equity history should survive ledger re-initialization."""
        db_path = tmp_path / "ledger.db"

        ledger1 = PortfolioLedger(db_path)
        ledger1.initialize()
        state = PortfolioState(cash=10000, equity=10000)
        ledger1.record_equity_snapshot(state)

        # New ledger instance, same db
        ledger2 = PortfolioLedger(db_path)
        rows = ledger2.list_equity_history()
        assert len(rows) == 1
        assert rows[0]["equity"] == 10000
