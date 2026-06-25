"""
Broker Adapter Base Class - V3.1 Task 1

Abstract interface for all broker implementations.
Ensures consistent API across Paper, Robinhood, and future brokers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, auto
from typing import Any


class BrokerMode(Enum):
    """Trading mode safety levels."""
    PAPER = auto()      # Simulated trading
    SHADOW = auto()     # Log intentions, don't execute
    LIVE = auto()       # Real money (requires explicit enable)


class OrderStatus(Enum):
    """Order execution status."""
    PENDING = auto()
    FILLED = auto()
    PARTIAL = auto()
    REJECTED = auto()
    CANCELLED = auto()


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


@dataclass(frozen=True)
class BrokerAccount:
    """Account information from broker."""
    account_id: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    currency: str = "USD"
    timestamp: datetime | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "cash": float(self.cash),
            "equity": float(self.equity),
            "buying_power": float(self.buying_power),
            "currency": self.currency,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass(frozen=True)
class BrokerPosition:
    """Position held at broker."""
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    current_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    timestamp: datetime | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": float(self.quantity),
            "avg_entry_price": float(self.avg_entry_price),
            "current_price": float(self.current_price) if self.current_price else None,
            "market_value": float(self.market_value) if self.market_value else None,
            "unrealized_pnl": float(self.unrealized_pnl) if self.unrealized_pnl else None,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


@dataclass(frozen=True)
class BrokerOrder:
    """Order at broker."""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    filled_quantity: Decimal
    status: OrderStatus
    price: Decimal | None = None  # Limit price or fill price
    created_at: datetime | None = None
    updated_at: datetime | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": float(self.quantity),
            "filled_quantity": float(self.filled_quantity),
            "status": self.status.name,
            "price": float(self.price) if self.price else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@dataclass(frozen=True)
class OrderPreview:
    """Preview of what an order would look like."""
    symbol: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    estimated_price: Decimal
    estimated_total: Decimal
    estimated_fees: Decimal
    buying_power_impact: Decimal
    warnings: list[str]
    timestamp: datetime | None = None
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": float(self.quantity),
            "order_type": self.order_type.value,
            "estimated_price": float(self.estimated_price),
            "estimated_total": float(self.estimated_total),
            "estimated_fees": float(self.estimated_fees),
            "buying_power_impact": float(self.buying_power_impact),
            "warnings": self.warnings,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }


class BrokerAdapter(ABC):
    """
    Abstract base class for all broker adapters.
    
    Implementations:
    - PaperBrokerAdapter: Simulated trading (existing)
    - RobinhoodAdapter: Read-only in V3, live in V4
    
    Safety guarantees:
    - submit_order() requires explicit mode parameter
    - Live mode requires explicit enable flag
    - All operations logged to audit trail
    """
    
    def __init__(self, mode: BrokerMode, config: dict[str, Any]):
        self.mode = mode
        self.config = config
        self._live_enabled = False  # Must explicitly enable
    
    @property
    def is_live(self) -> bool:
        """True if this adapter can place live orders."""
        return self.mode == BrokerMode.LIVE and self._live_enabled
    
    def enable_live(self) -> None:
        """
        Enable live trading. Must be called explicitly.
        Raises if mode is not LIVE.
        """
        if self.mode != BrokerMode.LIVE:
            raise RuntimeError(f"Cannot enable live trading in {self.mode.name} mode")
        self._live_enabled = True
    
    # ==================== Read-Only Operations (V3) ====================
    
    @abstractmethod
    def is_authenticated(self) -> bool:
        """Check if currently authenticated with broker."""
        pass
    
    @abstractmethod
    def get_account(self) -> BrokerAccount:
        """Fetch account information from broker."""
        pass
    
    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        """Fetch all positions from broker."""
        pass
    
    @abstractmethod
    def get_orders(self, since: datetime | None = None) -> list[BrokerOrder]:
        """Fetch recent orders from broker."""
        pass
    
    @abstractmethod
    def get_order(self, order_id: str) -> BrokerOrder | None:
        """Fetch specific order by ID."""
        pass
    
    @abstractmethod
    def is_tradable(self, symbol: str) -> bool:
        """Check if symbol is tradable on this broker."""
        pass
    
    @abstractmethod
    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get current quote for symbol."""
        pass
    
    # ==================== Trading Operations (V3 Shadow, V4 Live) ====================
    
    @abstractmethod
    def preview_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> OrderPreview:
        """
        Preview what an order would look like without submitting.
        
        Safe to call in any mode. Never executes a trade.
        """
        pass
    
    @abstractmethod
    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        order_type: OrderType = OrderType.MARKET,
        price: Decimal | None = None,
    ) -> BrokerOrder:
        """
        Submit an order to the broker.
        
        SAFETY: This method enforces mode constraints:
        - PAPER mode: Simulates fill
        - SHADOW mode: Logs intention, returns mock order, NO execution
        - LIVE mode: Only executes if enable_live() was called
        
        Raises:
            RuntimeError: If attempting live trade without explicit enable
        """
        pass
    
    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        pass
    
    # ==================== Utility ====================
    
    @abstractmethod
    def connect(self) -> bool:
        """Establish connection to broker."""
        pass
    
    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to broker."""
        pass
