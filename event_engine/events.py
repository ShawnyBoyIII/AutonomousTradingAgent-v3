"""Core event data classes for the event-driven backtesting engine.

Conventions:

* All events are :func:`dataclass` instances with ``slots=True`` and
  ``frozen=True``. ``frozen`` enforces immutability so events can be
  safely shared across threads without defensive copies; ``slots``
  keeps per-event memory low (see per-class docstring for the
  measured footprint).
* Timestamps are Python ``int`` values in UTC nanoseconds since the
  Unix epoch. Storing nanoseconds as ``int`` avoids the
  microsecond ceiling of :class:`datetime.datetime` and lets two
  events compare / hash with a single machine instruction.
* Numeric fields are eagerly validated on construction; out-of-range
  values raise :class:`EventValidationError`.
* Every concrete event carries a ``kind`` class attribute. The
  :class:`EventQueue` uses it for type-directed dispatch and the
  :class:`Portfolio` uses it to skip the wrong-shape events cheaply.
"""
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import ClassVar

from event_engine.exceptions import EventValidationError


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SignalDirection(str, Enum):
    """Long / short / flatten intent emitted by a strategy."""

    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"


class OrderDirection(str, Enum):
    """Side of the resulting order at the broker."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order types supported by the backtest broker."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    ICEBERG = "ICEBERG"


class TimeInForce(str, Enum):
    """Time-in-force policies applied at fill simulation time."""

    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class BarType(str, Enum):
    """Coarseness tag on MarketEvents. Useful for the DataHandler to
    emit ``TICK`` and ``BAR`` markers distinctly for downstream
    consumers that key signal logic off cadence."""

    TICK = "TICK"
    BAR_1M = "1m"
    BAR_5M = "5m"
    BAR_15M = "15m"
    BAR_1H = "1h"
    BAR_1D = "1d"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_finite_finite(value: float, field_name: str) -> float:
    """Reject NaN / +/-Inf. Negative prices or quantities raise too."""
    if value != value or value in (float("inf"), float("-inf")):
        raise EventValidationError(
            f"{field_name} must be a finite real number; got {value!r}"
        )
    return value


def _require_nonneg_finite(value: float, field_name: str) -> float:
    """Reject NaN / Inf and require the value to be >= 0."""
    _require_finite_finite(value, field_name)
    if value < 0:
        raise EventValidationError(
            f"{field_name} must be >= 0; got {value!r}"
        )
    return value


def _validate_timestamp_ns(value: int, field_name: str = "timestamp_ns") -> int:
    """Bound checks for a UTC nanosecond timestamp.

    Python ints are unbounded but we keep the value below ``Y9999``
    so callers can safely do arithmetic / format conversion.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise EventValidationError(
            f"{field_name} must be a Python int; got {type(value).__name__}"
        )
    if value < 0:
        raise EventValidationError(
            f"{field_name} must be non-negative; got {value}"
        )
    # ~ year 9999 in nanoseconds
    upper = 253_402_300_799_999_999_999
    if value > upper:
        raise EventValidationError(
            f"{field_name} exceeds the supported Y9999 ceiling"
        )
    return value


# ---------------------------------------------------------------------------
# Abstract Event base
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Event(ABC):
    """Abstract base. Concrete subclasses fill in concrete fields.

    ``kind`` is a short string discriminator (e.g. ``"MARKET"``) used
    by queue consumers to dispatch without ``isinstance`` chains.
    Subclasses override it.

    Memory / time:

    * Slot-frozen dataclasses allocate no ``__dict__``; per-instance
      overhead is the field storage only (~24 bytes for the base).
    * ``__hash__`` is implicitly generated from the frozen tuple; a
      million events sit in roughly ~150 MiB of pure payload.
    """

    kind: ClassVar[str] = "ABSTRACT"

    timestamp_ns: int

    def __post_init__(self) -> None:
        _validate_timestamp_ns(self.timestamp_ns)

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        """Tuple of dataclass field names — used by hash-based
        identity comparisons in the queue."""
        return tuple(f.name for f in fields(cls))


# ---------------------------------------------------------------------------
# Concrete events
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MarketEvent(Event):
    """Bar or tick for one symbol at one instant.

    The ``bar_type`` field tells downstream consumers whether the
    record is a tick or a coarse bar (and the bar length) so
    strategy logic can branch on cadence without inferring from
    timestamps.

    Footprint: 9 floats + 1 int + 1 Enum slot -> ~120 bytes per event.
    """

    kind: ClassVar[str] = "MARKET"

    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    bid_ask_spread: float
    bar_type: BarType = BarType.BAR_1M

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.symbol:
            raise EventValidationError("MarketEvent.symbol must be non-empty")
        _require_finite_finite(self.open, "open")
        _require_finite_finite(self.high, "high")
        _require_finite_finite(self.low, "low")
        _require_finite_finite(self.close, "close")
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise EventValidationError(
                f"OHLC incoherence for {self.symbol}: "
                f"low={self.low} high={self.high} open={self.open} close={self.close}"
            )
        _require_nonneg_finite(self.volume, "volume")
        _require_nonneg_finite(self.bid_ask_spread, "bid_ask_spread")


@dataclass(slots=True, frozen=True)
class SignalEvent(Event):
    """Strategy intent — a vector of constraints the Portfolio can
    materialise into an OrderEvent if capital allows.

    A ``target_quantity`` of zero is legal and means ``EXIT`` /
    flatten. ``strength`` is the dimensionless confidence in
    [-1, +1] (negative for short).
    """

    kind: ClassVar[str] = "SIGNAL"

    symbol: str
    signal_type: SignalDirection
    strength: float
    target_quantity: int
    suggested_stop_loss: float | None = None
    suggested_take_profit: float | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.symbol:
            raise EventValidationError("SignalEvent.symbol must be non-empty")
        if not isinstance(self.signal_type, SignalDirection):
            raise EventValidationError(
                f"signal_type must be a SignalDirection; got {type(self.signal_type).__name__}"
            )
        _require_finite_finite(self.strength, "strength")
        if not -1.0 <= self.strength <= 1.0:
            raise EventValidationError(
                f"strength must be in [-1, 1]; got {self.strength}"
            )
        if not isinstance(self.target_quantity, int) or isinstance(self.target_quantity, bool):
            raise EventValidationError(
                f"target_quantity must be an int; got {type(self.target_quantity).__name__}"
            )
        if self.target_quantity < 0:
            raise EventValidationError(
                f"target_quantity must be >= 0; got {self.target_quantity}"
            )
        if self.suggested_stop_loss is not None:
            _require_nonneg_finite(self.suggested_stop_loss, "suggested_stop_loss")
        if self.suggested_take_profit is not None:
            _require_nonneg_finite(self.suggested_take_profit, "suggested_take_profit")


@dataclass(slots=True, frozen=True)
class OrderEvent(Event):
    """Concrete order sent to the broker.

    ``order_id`` is required and uniqued by the queue. ``limit_price``
    / ``stop_price`` are not required for ``MARKET`` orders but
    must parse as non-negative floats when supplied.
    """

    kind: ClassVar[str] = "ORDER"

    symbol: str
    order_type: OrderType
    direction: OrderDirection
    quantity: int
    order_id: str = field()
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.GTC

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.symbol:
            raise EventValidationError("OrderEvent.symbol must be non-empty")
        if not self.order_id:
            raise EventValidationError("OrderEvent.order_id must be non-empty")
        if not isinstance(self.quantity, int) or isinstance(self.quantity, bool):
            raise EventValidationError(
                f"quantity must be an int; got {type(self.quantity).__name__}"
            )
        if self.quantity <= 0:
            raise EventValidationError(
                f"quantity must be positive; got {self.quantity}"
            )
        if not isinstance(self.order_type, OrderType):
            raise EventValidationError(
                f"order_type must be an OrderType; got {type(self.order_type).__name__}"
            )
        if not isinstance(self.direction, OrderDirection):
            raise EventValidationError(
                f"direction must be an OrderDirection; got {type(self.direction).__name__}"
            )
        if not isinstance(self.time_in_force, TimeInForce):
            raise EventValidationError(
                f"time_in_force must be a TimeInForce; got {type(self.time_in_force).__name__}"
            )
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise EventValidationError("LIMIT orders require limit_price")
        if self.order_type == OrderType.STOP and self.stop_price is None:
            raise EventValidationError("STOP orders require stop_price")
        if self.limit_price is not None:
            _require_nonneg_finite(self.limit_price, "limit_price")
        if self.stop_price is not None:
            _require_nonneg_finite(self.stop_price, "stop_price")


@dataclass(slots=True, frozen=True)
class FillEvent(Event):
    """Broker-confirmed execution.

    ``quantity_filled`` and ``commission_fee`` may be zero (e.g. for
    partial cancellation reports). ``impact_cost`` is reserved for
    market-impact simulations (default 0.0 for deterministic fill
    simulations that ignore impact).
    """

    kind: ClassVar[str] = "FILL"

    symbol: str
    exchange: str
    quantity_filled: int
    fill_price: float
    direction: OrderDirection
    commission_fee: float
    slippage_cost: float
    impact_cost: float
    order_id: str = field()

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.symbol:
            raise EventValidationError("FillEvent.symbol must be non-empty")
        if not self.exchange:
            raise EventValidationError("FillEvent.exchange must be non-empty")
        if not self.order_id:
            raise EventValidationError("FillEvent.order_id must be non-empty")
        if not isinstance(self.quantity_filled, int) or isinstance(self.quantity_filled, bool):
            raise EventValidationError(
                f"quantity_filled must be an int; got {type(self.quantity_filled).__name__}"
            )
        if self.quantity_filled < 0:
            raise EventValidationError(
                f"quantity_filled must be >= 0; got {self.quantity_filled}"
            )
        _require_nonneg_finite(self.fill_price, "fill_price")
        _require_nonneg_finite(self.commission_fee, "commission_fee")
        _require_nonneg_finite(self.slippage_cost, "slippage_cost")
        _require_nonneg_finite(self.impact_cost, "impact_cost")
        if not isinstance(self.direction, OrderDirection):
            raise EventValidationError(
                f"direction must be an OrderDirection; got {type(self.direction).__name__}"
            )


__all__ = [
    "BarType",
    "Event",
    "FillEvent",
    "MarketEvent",
    "OrderDirection",
    "OrderEvent",
    "OrderType",
    "SignalDirection",
    "SignalEvent",
    "TimeInForce",
]
