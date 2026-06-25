"""Robinhood broker package (MCP-only).

The supported path is the `RobinhoodBrokerBoundary`, which subclasses
`BrokerAdapter` and implements the read-only/shadow methods against
operator-synced JSON snapshots. There is no direct Robinhood auth, no
HTTP order submission, and no live-trading guard in this package:
    * credentials live in the operator-managed MCP server,
    * order submission emits structured intent records for later review,
    * `submit_order`/`cancel_order` raise NotImplementedError locally.

Legacy direct-auth/order/live-guard modules were removed when this package
collapsed to the MCP-only path.
"""

from trading_bot.brokers.robinhood.boundary import (
    BrokerAccountSummary,
    BrokerCapabilities,
    BrokerConnectionStatus,
    BrokerIntentRecord,
    BrokerIntentResult,
    BrokerOrderSummary,
    BrokerPositionSummary,
    BrokerQuoteSnapshot,
    RobinhoodBrokerBoundary,
)

__all__ = [
    "RobinhoodBrokerBoundary",
    "BrokerCapabilities",
    "BrokerConnectionStatus",
    "BrokerAccountSummary",
    "BrokerPositionSummary",
    "BrokerOrderSummary",
    "BrokerQuoteSnapshot",
    "BrokerIntentRecord",
    "BrokerIntentResult",
]
