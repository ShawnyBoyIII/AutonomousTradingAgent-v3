"""Tests for real-time P&L tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trading_bot.monitoring.realtime_pnl import (
    PnLAlertThresholds,
    RealTimePnL,
    calculate_pnl_change,
    calculate_realtime_pnl,
    check_pnl_alerts,
    format_pnl_snapshot,
)


class TestRealTimePnL:
    """Tests for real-time P&L calculations."""

    def test_calculate_realtime_pnl_basic(self) -> None:
        """Test basic P&L calculation."""
        mock_ledger = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.total_equity = 10000.0
        mock_portfolio.cash = 5000.0
        mock_portfolio.positions = {}
        mock_ledger.ensure_portfolio_state.return_value = mock_portfolio
        mock_ledger.list_order_rows.return_value = []

        current_prices = {}
        result = calculate_realtime_pnl(mock_ledger, current_prices)

        assert result.total_equity == 10000.0
        assert result.cash == 5000.0
        assert result.open_positions == 0

    def test_calculate_realtime_pnl_with_positions(self) -> None:
        """Test P&L calculation with open positions."""
        mock_ledger = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.total_equity = 15000.0
        mock_portfolio.cash = 5000.0

        # Create mock position
        mock_position = MagicMock()
        mock_position.quantity = 10
        mock_position.average_cost = 100.0
        mock_portfolio.positions = {"AAPL": mock_position}

        mock_ledger.ensure_portfolio_state.return_value = mock_portfolio
        mock_ledger.list_order_rows.return_value = []

        current_prices = {"AAPL": 110.0}  # $10 profit per share
        result = calculate_realtime_pnl(mock_ledger, current_prices)

        assert result.invested == 1100.0  # 10 shares * $110
        assert result.unrealized_pnl == 100.0  # 10 shares * $10 profit
        assert result.open_positions == 1

    def test_calculate_realtime_pnl_with_closed_trades(self) -> None:
        """Test P&L calculation with realized gains."""
        mock_ledger = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.total_equity = 10000.0
        mock_portfolio.cash = 10000.0
        mock_portfolio.positions = {}

        # Mock closed trades
        mock_ledger.ensure_portfolio_state.return_value = mock_portfolio
        mock_ledger.list_order_rows.return_value = [
            {"filled_at": datetime.now(timezone.utc).isoformat(), "side": "SELL", "pnl": 150.0},
            {"filled_at": datetime.now(timezone.utc).isoformat(), "side": "SELL", "pnl": -50.0},
        ]

        result = calculate_realtime_pnl(mock_ledger, {})

        assert result.realized_pnl == 100.0  # 150 - 50
        assert result.total_pnl == 100.0

    def test_calculate_realtime_pnl_heat_calculation(self) -> None:
        """Test portfolio heat calculation with losing positions."""
        mock_ledger = MagicMock()
        mock_portfolio = MagicMock()
        mock_portfolio.total_equity = 10000.0
        mock_portfolio.cash = 5000.0

        mock_position = MagicMock()
        mock_position.quantity = 10
        mock_position.average_cost = 100.0
        mock_portfolio.positions = {"AAPL": mock_position}

        mock_ledger.ensure_portfolio_state.return_value = mock_portfolio
        mock_ledger.list_order_rows.return_value = []

        current_prices = {"AAPL": 95.0}  # $5 loss per share = $50 total loss
        result = calculate_realtime_pnl(mock_ledger, current_prices)

        assert result.unrealized_pnl == -50.0
        assert result.positions_heat == 0.5  # 50/10000 * 100


class TestFormatPnLSnapshot:
    """Tests for P&L snapshot formatting."""

    def test_format_pnl_snapshot(self) -> None:
        """Test snapshot formatting."""
        snapshot = RealTimePnL(
            timestamp=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            total_equity=10000.0,
            cash=5000.0,
            invested=5000.0,
            realized_pnl=100.0,
            unrealized_pnl=50.0,
            total_pnl=150.0,
            today_trades=5,
            today_pnl=75.0,
            open_positions=2,
            positions_heat=1.5,
            alerts=["Test alert"],
        )

        formatted = format_pnl_snapshot(snapshot)

        assert "timestamp" in formatted
        assert "equity" in formatted
        assert formatted["equity"]["total"] == 10000.0
        assert formatted["pnl"]["total"] == 150.0
        assert formatted["trading"]["today_trades"] == 5
        assert len(formatted["alerts"]) == 1


class TestCheckPnLAlerts:
    """Tests for P&L alert checking."""

    def test_check_pnl_alerts_daily_loss(self) -> None:
        """Test daily loss alert."""
        snapshot = RealTimePnL(
            timestamp=datetime.now(timezone.utc),
            total_equity=10000.0,
            cash=3000.0,  # 30% cash - avoid low cash alert
            today_pnl=-1500.0,  # Exceeds -1000 limit
        )

        thresholds = PnLAlertThresholds(max_daily_loss=-1000.0)
        alerts = check_pnl_alerts(snapshot, thresholds)

        loss_alerts = [a for a in alerts if a["type"] == "daily_loss_limit"]
        assert len(loss_alerts) == 1
        assert loss_alerts[0]["level"] == "critical"

    def test_check_pnl_alerts_high_heat(self) -> None:
        """Test portfolio heat alert."""
        snapshot = RealTimePnL(
            timestamp=datetime.now(timezone.utc),
            total_equity=10000.0,
            positions_heat=5.0,  # Exceeds 3% limit
        )

        thresholds = PnLAlertThresholds(max_positions_heat_pct=3.0)
        alerts = check_pnl_alerts(snapshot, thresholds)

        heat_alerts = [a for a in alerts if a["type"] == "high_portfolio_heat"]
        assert len(heat_alerts) == 1
        assert heat_alerts[0]["level"] == "warning"

    def test_check_pnl_alerts_no_alerts(self) -> None:
        """Test that normal values produce no alerts."""
        snapshot = RealTimePnL(
            timestamp=datetime.now(timezone.utc),
            total_equity=10000.0,
            cash=3000.0,  # 30% cash - above 10% minimum
            today_pnl=-500.0,  # Within -1000 limit
            positions_heat=2.0,  # Within 3% limit
        )

        thresholds = PnLAlertThresholds()
        alerts = check_pnl_alerts(snapshot, thresholds)

        assert len(alerts) == 0

    def test_check_pnl_alerts_low_cash(self) -> None:
        """Test low cash buffer alert."""
        snapshot = RealTimePnL(
            timestamp=datetime.now(timezone.utc),
            total_equity=10000.0,
            cash=500.0,  # Only 5% - below 10% minimum
        )

        thresholds = PnLAlertThresholds(min_equity_buffer_pct=10.0)
        alerts = check_pnl_alerts(snapshot, thresholds)

        cash_alerts = [a for a in alerts if a["type"] == "low_cash_buffer"]
        assert len(cash_alerts) == 1


class TestCalculatePnLChange:
    """Tests for P&L change calculations."""

    def test_calculate_pnl_change_with_previous(self) -> None:
        """Test change calculation between two snapshots."""
        current = RealTimePnL(
            timestamp=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            total_equity=11000.0,
            total_pnl=1000.0,
        )
        previous = RealTimePnL(
            timestamp=datetime(2025, 1, 1, 11, 0, 0, tzinfo=timezone.utc),
            total_equity=10000.0,
            total_pnl=500.0,
        )

        change = calculate_pnl_change(current, previous)

        assert change["equity_change"] == 1000.0
        assert change["equity_change_pct"] == 10.0
        assert change["pnl_change"] == 500.0
        assert change["time_delta_seconds"] == 3600.0

    def test_calculate_pnl_change_no_previous(self) -> None:
        """Test change calculation with no previous snapshot."""
        current = RealTimePnL(
            timestamp=datetime.now(timezone.utc),
            total_equity=10000.0,
            total_pnl=500.0,
        )

        change = calculate_pnl_change(current, None)

        assert change["equity_change"] == 0.0
        assert change["equity_change_pct"] == 0.0
        assert change["time_delta_seconds"] == 0.0
