"""Tests for position reconciliation against broker snapshots."""

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

from trading_bot.brokers.base import BrokerPosition
from trading_bot.brokers.robinhood.reconciliation import (
    PositionReconciler,
    ReconciliationResult,
    reconcile_positions,
)
from trading_bot.models.portfolio import PortfolioState, Position


def _mock_ledger(positions: dict[str, Position], cash: float = 10_000.0) -> MagicMock:
    ledger = MagicMock()
    ledger.ensure_portfolio_state.return_value = PortfolioState(
        cash=cash,
        equity=cash + sum(p.quantity * p.average_cost for p in positions.values()),
        positions=positions,
    )
    return ledger


def _broker_position(symbol: str, quantity: float, avg_cost: float) -> BrokerPosition:
    return BrokerPosition(
        symbol=symbol,
        quantity=__import__("decimal").Decimal(str(quantity)),
        avg_entry_price=__import__("decimal").Decimal(str(avg_cost)),
        current_price=__import__("decimal").Decimal(str(avg_cost)),
        market_value=__import__("decimal").Decimal(str(quantity * avg_cost)),
    )


def _boundary_with(positions: list[BrokerPosition], source: str = "mcp") -> MagicMock:
    boundary = MagicMock()
    boundary.get_positions.return_value = positions
    status = MagicMock()
    status.source = source
    boundary.get_status.return_value = status
    return boundary


def test_reconciliation_matches_when_quantities_equal() -> None:
    ledger = _mock_ledger({"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)})
    boundary = _boundary_with([_broker_position("AAPL", 5, 100.0)])

    reconciler = PositionReconciler(ledger, boundary)
    result = reconciler.reconcile_positions(tolerance_pct=1.0)

    assert result.matches is True
    assert result.discrepancies == []
    assert result.broker_source == "mcp"


def test_reconciliation_flags_broker_only_position() -> None:
    ledger = _mock_ledger({"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)})
    boundary = _boundary_with([
        _broker_position("AAPL", 5, 100.0),
        _broker_position("MSFT", 10, 200.0),
    ])

    result = PositionReconciler(ledger, boundary).reconcile_positions()

    assert result.matches is False
    assert "MSFT" in result.broker_only
    assert len(result.discrepancies) == 1
    assert result.discrepancies[0].symbol == "MSFT"


def test_reconciliation_flags_local_only_position() -> None:
    ledger = _mock_ledger({
        "AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0),
        "TSLA": Position(ticker="TSLA", quantity=2, average_cost=300.0),
    })
    boundary = _boundary_with([_broker_position("AAPL", 5, 100.0)])

    result = PositionReconciler(ledger, boundary).reconcile_positions()

    assert result.matches is False
    assert "TSLA" in result.local_only


def test_reconciliation_flags_quantity_mismatch() -> None:
    ledger = _mock_ledger({"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)})
    boundary = _boundary_with([_broker_position("AAPL", 10, 100.0)])

    result = PositionReconciler(ledger, boundary).reconcile_positions(tolerance_pct=1.0)

    assert result.matches is False
    assert len(result.discrepancies) == 1
    assert result.discrepancies[0].difference == -5


def test_reconciliation_without_broker_client_reports_disconnected() -> None:
    ledger = _mock_ledger({"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)})

    result = PositionReconciler(ledger, None).reconcile_positions()

    assert result.matches is False
    assert result.broker_source == "none"
    assert result.broker_total_value == 0.0


def test_reconciliation_report_contains_source_and_status() -> None:
    ledger = _mock_ledger({"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)})
    boundary = _boundary_with([_broker_position("AAPL", 5, 100.0)], source="mcp")

    result = PositionReconciler(ledger, boundary).reconcile_positions()
    report = PositionReconciler(ledger, boundary).generate_reconciliation_report(result)

    assert "Broker Source: mcp" in report
    assert "MATCHED" in report


def test_reconcile_positions_convenience_fn_uses_boundary() -> None:
    ledger = _mock_ledger({"AAPL": Position(ticker="AAPL", quantity=5, average_cost=100.0)})
    boundary = _boundary_with([_broker_position("AAPL", 5, 100.0)])

    result = reconcile_positions(ledger, boundary, tolerance_pct=1.0)

    assert isinstance(result, ReconciliationResult)
    assert result.matches is True
