r"""Event-driven backtesting engine — Stages 1–4 (events + queue,
data handler + portfolio + execution handler, plus the strategy
interface, vectorized pre-filter, and engine driver).

This package is a *stand-alone* alternative to the live paper-trading
event system under ``trading_bot.events``. It uses pure stdlib (plus
``numpy`` and ``pandas`` for the data handler and pre-filter) and
dataclass-with-slots events instead of Pydantic, so backtests can
be expressed without taking on the live trading pipeline's runtime
dependencies.

Layout:

* :mod:`.events`     — abstract :class:`Event` and four concrete
  dataclasses (:class:`MarketEvent`, :class:`SignalEvent`,
  :class:`OrderEvent`, :class:`FillEvent`).
* :mod:`.queue`      — :class:`EventQueue`, a thread-safe priority
  queue with temporal validation and order-id dedupe.
* :mod:`.handlers`   — :class:`AbstractDataHandler` and the CSV
  importer :class:`HistoricCSVDataHandler`.
* :mod:`.portfolio`  — :class:`Portfolio` and :class:`PortfolioPolicy`.
* :mod:`.execution`  — :class:`ExchangeHandler` ABC and the
  concrete :class:`SimulatedExecutionHandler` with Almgren-Chriss
  market impact, limit/stop order simulation, and partial fills.
* :mod:`.strategy`   — :class:`AbstractStrategy` interface and the
  :class:`BollingerZScoreReversionStrategy` sample.
* :mod:`.prefilter`  — :class:`VectorizedPreFilter` for fast
  parameter sweeps in pure pandas / NumPy.
* :mod:`.engine`     — :class:`EngineDriver` orchestrator with
  heartbeat, signal handler, and context-manager ergonomics.
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
from event_engine.execution import (
    AlmgrenChrissParams,
    ExchangeHandler,
    ExecutionError,
    ImpactDecomposition,
    InsufficientLiquidityError,
    RestingOrder,
    SimulatedExchangeConfig,
    SimulatedExecutionHandler,
    decompose_impact,
    permanent_impact_per_unit,
    square_root_impact_per_unit,
    temporary_impact_per_unit,
)
from event_engine.queue import EventQueue
from event_engine.strategy import (
    AbstractStrategy,
    BollingerZScoreReversionStrategy,
)
from event_engine.prefilter import (
    PreFilterParameterScore,
    PreFilterResult,
    VectorizedPreFilter,
)
from event_engine.engine import (
    DriverHeartbeat,
    DriverRunResult,
    EngineDriver,
)

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
    # Stage 3: execution
    "ExchangeHandler",
    "SimulatedExecutionHandler",
    "AlmgrenChrissParams",
    "SimulatedExchangeConfig",
    "ImpactDecomposition",
    "RestingOrder",
    "permanent_impact_per_unit",
    "temporary_impact_per_unit",
    "square_root_impact_per_unit",
    "decompose_impact",
    # Stage 4: strategy / prefilter / engine
    "AbstractStrategy",
    "BollingerZScoreReversionStrategy",
    "VectorizedPreFilter",
    "PreFilterParameterScore",
    "PreFilterResult",
    "EngineDriver",
    "DriverHeartbeat",
    "DriverRunResult",
    # Errors
    "DataHandlerError",
    "DuplicateOrderIdError",
    "EventEngineError",
    "EventValidationError",
    "ExecutionError",
    "InsufficientLiquidityError",
    "PointInTimeLeakError",
    "QueueError",
    "QueuePoisonedError",
    "QueueStarvationError",
    "TemporalSequenceViolationError",
    "UnknownSymbolError",
]

__version__ = "0.4.0"
