"""
Broker abstraction layer for V3 Robinhood integration.

Provides abstract base class and concrete implementations for different brokers.
All broker interactions go through this layer for safety and consistency.
"""

from trading_bot.brokers.base import BrokerAdapter, OrderPreview, BrokerAccount, BrokerPosition, BrokerOrder
from trading_bot.brokers.paper import PaperBrokerAdapter

__all__ = [
    "BrokerAdapter",
    "OrderPreview", 
    "BrokerAccount",
    "BrokerPosition",
    "BrokerOrder",
    "PaperBrokerAdapter",
]
