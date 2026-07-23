"""
Broker abstraction layer for V3 Robinhood integration.

Provides abstract base class for read-only broker snapshots and
reconciliation. The active execution path uses trading_bot.execution
(PaperBroker + OrderRequest/FillResult) directly. The Robinhood
boundary subclasses BrokerAdapter for read-only position and order
status queries; no live trading path uses the abstract base for
order submission.
"""

from trading_bot.brokers.base import BrokerAdapter, OrderPreview, BrokerAccount, BrokerPosition, BrokerOrder

__all__ = [
    "BrokerAdapter",
    "OrderPreview",
    "BrokerAccount",
    "BrokerPosition",
    "BrokerOrder",
]
