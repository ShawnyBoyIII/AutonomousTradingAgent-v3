"""Exception hierarchy for the event-driven backtesting engine.

All exceptions raised by the event_engine package inherit from
:class:`EventEngineError`, so callers can install a single broad
``except`` and still benefit from narrower subclasses for specific
recovery / error-reporting paths.

The hierarchy intentionally separates **infrastructure** errors
(queue, threading) from **domain** errors (validation, sequencing,
capital) — both useful, but the latter is what a backtest operator
will normally want to surface while the former is more often a
bug in the harness.
"""
from __future__ import annotations


class EventEngineError(Exception):
    """Root of the event_engine exception hierarchy."""


# ---------------------------------------------------------------------------
# Infrastructure errors
# ---------------------------------------------------------------------------


class QueueError(EventEngineError):
    """Base for queue- and threading-level failures."""


class QueueStarvationError(QueueError):
    """Raised when an ``EventQueue.get`` call blocks past its timeout
    while the queue appears non-empty.

    Production backtests with tight latency budgets should treat this
    as a critical signal — either the producer is stalled or the
    priority comparator is mis-ordering events so the consumer can
    never reach the head.
    """


class QueuePoisonedError(QueueError):
    """Raised on ``EventQueue.get`` after :meth:`poison` has been
    called. Lets every drained worker terminate on a single
    well-known exception.
    """


# ---------------------------------------------------------------------------
# Event-content errors
# ---------------------------------------------------------------------------


class EventValidationError(EventEngineError):
    """Raised when an event's attributes violate construction-time
    invariants (negative quantity, NaN price, malformed direction).
    """


class TemporalSequenceViolationError(EventEngineError):
    """Raised by :class:`EventQueue` when an incoming event's
    timestamp is strictly less than the most-recently-consumed
    timestamp.

    The queue rejects the offending event so the backtest fails
    closed; auto-resequencing hides data-feed bugs and is explicitly
    *not* performed here (per design decision).
    """


class DuplicateOrderIdError(EventValidationError):
    """Raised by :class:`EventQueue` when an :class:`OrderEvent`
    already-known ``order_id`` is seen a second time. Two events
    cannot describe the same order — silent collisions are a classic
    source of double-fill errors.
    """


# ---------------------------------------------------------------------------
# Stage 2 domain errors
# ---------------------------------------------------------------------------


class InsufficientCapitalError(EventEngineError):
    """Raised by :class:`Portfolio` when a SignalEvent would translate
    into an OrderEvent whose required cash (or margin, post-leverage)
    exceeds what the account can support.
    """


class DataHandlerError(EventEngineError):
    """Base for ingest / replay failures from a DataHandler."""


class PointInTimeLeakError(DataHandlerError):
    """Raised by a DataHandler when its strategy requests a bar whose
    timestamp is later than the simulator's current time — the
    classic symptom of lookahead bias.
    """


class UnknownSymbolError(DataHandlerError):
    """Raised when a DataHandler has no series registered for the
    requested symbol.
    """


__all__ = [
    "EventEngineError",
    "QueueError",
    "QueueStarvationError",
    "QueuePoisonedError",
    "EventValidationError",
    "TemporalSequenceViolationError",
    "DuplicateOrderIdError",
    "InsufficientCapitalError",
    "DataHandlerError",
    "PointInTimeLeakError",
    "UnknownSymbolError",
]
