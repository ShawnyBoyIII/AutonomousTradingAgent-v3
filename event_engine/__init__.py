"""Event-driven backtesting engine — Stage 1 (events + queue) and
Stage 2 (data handler + portfolio accounting).

This package is a *stand-alone* alternative to the live paper-trading
event system under ``trading_bot.events``. It uses pure stdlib (plus
``numpy`` for the data handler) and dataclass-with-slots events
instead of Pydantic, so backtests can be expressed without taking
on the live trading pipeline's runtime dependencies.

Layout:

* :mod:`.events`     — abstract :class:`Event` and four concrete
  dataclasses (:class:`MarketEvent`, :class:`SignalEvent`,
  :class:`OrderEvent`, :class:`FillEvent`).
* :mod:`.queue`      — :class:`EventQueue`, a thread-safe priority
  queue with temporal validation and order-id dedupe.
* :mod:`.handlers`   — :class:`AbstractDataHandler` and the CSV
  importer :class:`HistoricCSVDataHandler`.
* :mod:`.portfolio`  — :class:`Portfolio` and :class:`PortfolioPolicy`.
* :mod:`.exceptions` — typed error hierarchy.
"""
from event_engine.events import (
    BarType,
    Event,
    FillEvent,
    MarketEvent,
    OrderDirection,
    OrderEvent,
    OrderType,
    SignalDirection,
    SignalEvent,
    TimeInForce,
)
from event_engine.exceptions import (
    DataHandlerError,
    DuplicateOrderIdError,
    EventEngineError,
    EventValidationError,
    InsufficientCapitalError,
    PointInTimeLeakError,
    QueueError,
    QueuePoisonedError,
    QueueStarvationError,
    TemporalSequenceViolationError,
    UnknownSymbolError,
)
from event_engine.handlers import (
    AbstractDataHandler,
    HistoricCSVDataHandler,
)
from event_engine.portfolio import (
    Portfolio,
    PortfolioPolicy,
)
from event_engine.queue import EventQueue

__all__ = [
    # Stage 1: events + types
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
    # Stage 1: queue
    "EventQueue",
    # Stage 2: data handlers
    "AbstractDataHandler",
    "HistoricCSVDataHandler",
    # Stage 2: portfolio
    "Portfolio",
    "PortfolioPolicy",
    # Errors
    "DataHandlerError",
    "DuplicateOrderIdError",
    "EventEngineError",
    "EventValidationError",
    "InsufficientCapitalError",
    "PointInTimeLeakError",
    "QueueError",
    "QueuePoisonedError",
    "QueueStarvationError",
    "TemporalSequenceViolationError",
    "UnknownSymbolError",
]

__version__ = "0.1.0"
