"""Position Reconciliation - V3

Compares local portfolio state with broker account snapshots.
Detects and reports discrepancies. Read-only: never mutates state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from trading_bot.brokers.base import BrokerAdapter
    from trading_bot.brokers.robinhood.boundary import RobinhoodBrokerBoundary
    from trading_bot.portfolio.ledger import PortfolioLedger

logger = logging.getLogger(__name__)


@dataclass
class PositionDiscrepancy:
    """Difference between local and broker position."""

    symbol: str
    local_quantity: float
    broker_quantity: float
    difference: float
    local_value: float
    broker_value: float
    severity: str  # minor, major, critical


@dataclass
class ReconciliationResult:
    """Result of position reconciliation."""

    matches: bool
    discrepancies: list[PositionDiscrepancy] = field(default_factory=list)
    local_only: list[str] = field(default_factory=list)
    broker_only: list[str] = field(default_factory=list)
    local_total_value: float = 0.0
    broker_total_value: float = 0.0
    value_difference_pct: float = 0.0
    broker_source: str = "unknown"


class PositionReconciler:
    """Reconciles local positions with broker account snapshots.

    ``broker_client`` must implement the :class:`BrokerAdapter` interface
    (e.g. :class:`RobinhoodBrokerBoundary`). Read-only: this class never
    mutates either side of the comparison.
    """

    def __init__(self, ledger: "PortfolioLedger", broker_client: "BrokerAdapter | None") -> None:
        self.ledger = ledger
        self.broker_client = broker_client

    def reconcile_positions(self, tolerance_pct: float = 1.0) -> ReconciliationResult:
        """Compare local and broker positions.

        Args:
            tolerance_pct: Allowed difference % before flagging a discrepancy

        Returns:
            ReconciliationResult with all differences. ``matches`` is False
            if ``broker_client`` is None or any discrepancy exceeds tolerance.
        """
        local_positions = self._get_local_positions()
        broker_positions, broker_source = self._get_broker_positions()

        if not broker_positions and self.broker_client is not None:
            # Broker was provided but returned no positions (e.g. snapshot
            # not fresh). Report as a reconciliation failure rather than a
            # silent "everything matches".
            return ReconciliationResult(
                matches=False,
                broker_source=broker_source,
                local_total_value=sum(p["value"] for p in local_positions.values()),
                broker_total_value=0.0,
                value_difference_pct=100.0,
            )

        discrepancies: list[PositionDiscrepancy] = []
        local_only: list[str] = []
        broker_only: list[str] = []

        total_local_value = 0.0
        total_broker_value = 0.0

        all_symbols = set(local_positions.keys()) | set(broker_positions.keys())

        for symbol in all_symbols:
            local_pos = local_positions.get(symbol, {"quantity": 0, "value": 0})
            broker_pos = broker_positions.get(symbol, {"quantity": 0, "value": 0})

            local_qty = local_pos["quantity"]
            broker_qty = broker_pos["quantity"]
            local_val = local_pos["value"]
            broker_val = broker_pos["value"]

            total_local_value += local_val
            total_broker_value += broker_val

            if symbol not in local_positions:
                broker_only.append(symbol)
                discrepancies.append(PositionDiscrepancy(
                    symbol=symbol,
                    local_quantity=0,
                    broker_quantity=broker_qty,
                    difference=-broker_qty,
                    local_value=0,
                    broker_value=broker_val,
                    severity="major",
                ))
            elif symbol not in broker_positions:
                local_only.append(symbol)
                discrepancies.append(PositionDiscrepancy(
                    symbol=symbol,
                    local_quantity=local_qty,
                    broker_quantity=0,
                    difference=local_qty,
                    local_value=local_val,
                    broker_value=0,
                    severity="major",
                ))
            else:
                qty_diff = abs(local_qty - broker_qty)
                qty_diff_pct = (qty_diff / broker_qty * 100) if broker_qty > 0 else 0

                if qty_diff_pct > tolerance_pct or qty_diff >= 1.0:
                    severity = (
                        "critical" if qty_diff_pct > 10
                        else "major" if qty_diff_pct > 5
                        else "minor"
                    )
                    discrepancies.append(PositionDiscrepancy(
                        symbol=symbol,
                        local_quantity=local_qty,
                        broker_quantity=broker_qty,
                        difference=local_qty - broker_qty,
                        local_value=local_val,
                        broker_value=broker_val,
                        severity=severity,
                    ))

        value_diff_pct = 0.0
        if total_broker_value > 0:
            value_diff_pct = abs(total_local_value - total_broker_value) / total_broker_value * 100

        matches = len(discrepancies) == 0 and value_diff_pct < tolerance_pct

        if not matches:
            logger.warning(
                "Position reconciliation found %d discrepancies, value difference: %.2f%%",
                len(discrepancies),
                value_diff_pct,
            )

        return ReconciliationResult(
            matches=matches,
            discrepancies=discrepancies,
            local_only=local_only,
            broker_only=broker_only,
            local_total_value=total_local_value,
            broker_total_value=total_broker_value,
            value_difference_pct=value_diff_pct,
            broker_source=broker_source,
        )

    def _get_local_positions(self) -> dict[str, dict]:
        """Get positions from local ledger."""
        try:
            portfolio = self.ledger.ensure_portfolio_state()
            positions: dict[str, dict] = {}

            for symbol, position in portfolio.positions.items():
                positions[symbol] = {
                    "quantity": position.quantity,
                    "value": position.quantity * position.average_cost,
                    "avg_cost": position.average_cost,
                }

            return positions
        except Exception as e:
            logger.error("Error getting local positions: %s", e)
            return {}

    def _get_broker_positions(self) -> tuple[dict[str, dict], str]:
        """Get positions from broker via the BrokerAdapter interface.

        Returns ``(positions, source)`` where ``source`` identifies where the
        data came from (e.g. ``"mcp"``). Returns ``({}, "none")`` when no
        broker client is configured or the snapshot is unavailable.
        """
        if self.broker_client is None:
            return {}, "none"

        try:
            broker_positions = self.broker_client.get_positions()
        except Exception as e:
            logger.error("Error getting broker positions: %s", e)
            return {}, "error"

        source = getattr(self.broker_client, "settings", None)
        source_name = "broker"
        if source is not None and hasattr(source, "robinhood"):
            boundary = cast("RobinhoodBrokerBoundary", self.broker_client)
            status = boundary.get_status()
            source_name = status.source if status else "broker"

        positions: dict[str, dict] = {}
        for pos in broker_positions:
            quantity = float(pos.quantity)
            avg_cost = float(pos.avg_entry_price)
            market_value = float(pos.market_value) if pos.market_value is not None else quantity * avg_cost
            positions[pos.symbol] = {
                "quantity": quantity,
                "value": market_value,
                "avg_cost": avg_cost,
            }

        return positions, source_name

    def generate_reconciliation_report(self, result: ReconciliationResult) -> str:
        """Generate human-readable reconciliation report."""
        lines = [
            "Position Reconciliation Report",
            "=" * 50,
            f"Broker Source: {result.broker_source}",
            f"Status: {'MATCHED' if result.matches else 'DISCREPANCIES FOUND'}",
            f"Local Value:  ${result.local_total_value:,.2f}",
            f"Broker Value: ${result.broker_total_value:,.2f}",
            f"Difference:   {result.value_difference_pct:.2f}%",
            "",
        ]

        if result.discrepancies:
            lines.append("Discrepancies:")
            lines.append("-" * 50)
            for disc in result.discrepancies:
                lines.append(
                    f"{disc.symbol}: Local={disc.local_quantity:.2f}, "
                    f"Broker={disc.broker_quantity:.2f}, "
                    f"Diff={disc.difference:+.2f} [{disc.severity.upper()}]"
                )

        if result.local_only:
            lines.append(f"\nLocal Only: {', '.join(result.local_only)}")

        if result.broker_only:
            lines.append(f"\nBroker Only: {', '.join(result.broker_only)}")

        return "\n".join(lines)


def reconcile_positions(
    ledger: "PortfolioLedger",
    broker_client: "BrokerAdapter | None",
    tolerance_pct: float = 1.0,
) -> ReconciliationResult:
    """Convenience function for position reconciliation."""
    reconciler = PositionReconciler(ledger, broker_client)
    return reconciler.reconcile_positions(tolerance_pct)
