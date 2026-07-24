"""Thread-safe, priority-aware :class:`EventQueue`.

The queue is a counting-wrapper around :class:`queue.PriorityQueue`
that:

* Sorts deterministically by ``timestamp_ns`` so a multi-feed
  backtester sees events in causal order. Equal-timestamp events
  are ordered by a *seqno* assigned at ``put`` time so the queue
  remains total.
* Rejects inputs that violate temporal sequencing — an event whose
  timestamp is strictly less than the most-recently-consumed
  timestamp is detected as :class:`TemporalSequenceViolationError`.
  Auto-resequencing would hide data-feed bugs and is explicitly
  not performed (per design decision).
* Dedupes :class:`OrderEvent` by ``order_id``. Two events cannot
  describe the same order; the second raises
  :class:`DuplicateOrderIdError`.
* Supports a ``poison`` flag that drains all blocked workers on a
  single well-known :class:`QueuePoisonedError`.

Complexity:

* ``put``: ``O(log N)`` (heap insert), constant amortised overhead.
* ``get``: ``O(log N)`` (heap pop).
* Memory: one heap entry per put; the underlying tuple is small
  enough that a million events fit comfortably in well under 200 MiB.
"""
from __future__ import annotations

import itertools
import queue
import threading
from dataclasses import dataclass, field
from typing import Optional

from event_engine.events import Event, OrderEvent
from event_engine.exceptions import (
    DuplicateOrderIdError,
    EventValidationError,
    QueuePoisonedError,
    QueueStarvationError,
    TemporalSequenceViolationError,
)


@dataclass(order=True)
class _HeapEntry:
    """Sorting key in the priority queue.

    The ``event`` field is excluded from ordering so two events at the
    same timestamp break their tie by the ``seqno`` assigned at enqueue.
    """

    sort_key: tuple[int, int]
    event: Event = field(compare=False)


class EventQueue:
    """Priority-aware event queue with temporal validation.

    Parameters
    ----------
    maxsize:
        Upper bound on queued events. ``0`` means unbounded.
    enforce_temporal_order:
        When ``True`` (default), events whose timestamp is strictly
        less than the most-recently-consumed timestamp raise
        :class:`TemporalSequenceViolationError`. Pass ``False`` for
        live-pipeline mode when out-of-order arrivals are legal.
    """

    def __init__(
        self,
        maxsize: int = 0,
        enforce_temporal_order: bool = True,
    ) -> None:
        if maxsize < 0:
            raise EventValidationError("maxsize must be >= 0")
        self._maxsize = maxsize
        self._enforce_temporal_order = enforce_temporal_order
        self._heap: "queue.PriorityQueue[_HeapEntry]" = queue.PriorityQueue(
            maxsize=maxsize if maxsize > 0 else 0
        )
        self._seq = itertools.count()
        # ``_last_consumed_ns`` is ``None`` until the first get returns.
        self._last_consumed_ns: Optional[int] = None
        self._known_order_ids: set[str] = set()
        self._poisoned = threading.Event()
        self._lock = threading.RLock()
        self._unfinished_tasks = 0
        self._finished = threading.Semaphore(0)

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------

    def put(self, event: Event) -> None:
        """Enqueue ``event``. Validates order_id and temporal ordering.

        Raises
        ------
        DuplicateOrderIdError
            If ``event`` is an ``OrderEvent`` with an ``order_id``
            that has already been queued.
        TemporalSequenceViolationError
            If ``enforce_temporal_order`` is ``True`` and ``event``'s
            timestamp is strictly less than the most-recently-consumed
            timestamp (note: *consumed*, not *enqueued*).
        QueuePoisonedError
            If the queue has been poisoned via :meth:`poison`.
        queue.Full
            If ``maxsize`` is positive and the queue is full.
        """
        if self._poisoned.is_set():
            raise QueuePoisonedError("EventQueue has been poisoned")
        if not isinstance(event, Event):
            raise EventValidationError(
                f"expected an Event subclass; got {type(event).__name__}"
            )

        with self._lock:
            if isinstance(event, OrderEvent):
                if event.order_id in self._known_order_ids:
                    raise DuplicateOrderIdError(
                        f"order_id {event.order_id!r} already enqueued"
                    )
                self._known_order_ids.add(event.order_id)
            seq = next(self._seq)
            entry = _HeapEntry(sort_key=(event.timestamp_ns, seq), event=event)

        self._unfinished_tasks += 1
        try:
            self._heap.put(entry)
        except queue.Full:
            # Roll back the side effects on backpressure.
            with self._lock:
                if isinstance(event, OrderEvent) and event.order_id in self._known_order_ids:
                    self._known_order_ids.discard(event.order_id)
            self._unfinished_tasks -= 1
            raise

    def put_nowait(self, event: Event) -> None:
        """Same as :meth:`put` but raises ``queue.Full`` instead of
        blocking on a bounded queue."""
        if self._heap.full():
            raise queue.Full("EventQueue is full")
        self.put(event)

    # ------------------------------------------------------------------
    # Consumer API
    # ------------------------------------------------------------------

    def get(self, timeout: Optional[float] = None) -> Event:
        """Pop the event with the lowest ``(timestamp_ns, seq)``.

        ``timeout`` is in seconds; ``None`` blocks forever. ``0`` is a
        non-blocking poll that either returns immediately or raises
        :class:`QueueStarvationError`.

        Raises
        ------
        TemporalSequenceViolationError
            If ``enforce_temporal_order`` is ``True`` *and* the popped
            event's timestamp is strictly less than ``_last_consumed_ns``.
        QueueStarvationError
            If the queue is empty when ``timeout`` elapses (or for a
            ``timeout == 0`` non-blocking poll).
        QueuePoisonedError
            If the queue has been poisoned via :meth:`poison` before
            the call resolved.
        """
        if timeout is not None and timeout < 0:
            raise EventValidationError("timeout must be >= 0")

        # Non-blocking poll: hand off directly so the underlying
        # PriorityQueue decides immediately whether to return or
        # raise ``queue.Empty``.
        if timeout == 0:
            try:
                entry = self._heap.get_nowait()
            except queue.Empty:
                raise QueueStarvationError(
                    "no event available (timeout=0)"
                )
            self._finalize_pop(entry)
            return entry.event

        deadline: Optional[float] = None
        if timeout is not None:
            deadline = _monotonic() + timeout

        while True:
            if self._poisoned.is_set():
                raise QueuePoisonedError("EventQueue has been poisoned")
            remaining: Optional[float]
            if deadline is None:
                remaining = None
            else:
                remaining = max(0.0, deadline - _monotonic())
                if remaining == 0.0:
                    raise QueueStarvationError(
                        f"no event arrived within {timeout}s"
                    )
            try:
                entry = self._heap.get(timeout=remaining)
                break
            except queue.Empty:
                raise QueueStarvationError(
                    f"no event arrived within {timeout}s"
                )

        self._finalize_pop(entry)
        return entry.event

    def _finalize_pop(self, entry: "_HeapEntry") -> None:
        """Side-effects of a successful pop: temporal bookkeeping
        and unfinished-tasks accounting."""
        if self._enforce_temporal_order:
            with self._lock:
                if (
                    self._last_consumed_ns is not None
                    and entry.event.timestamp_ns < self._last_consumed_ns
                ):
                    raise TemporalSequenceViolationError(
                        f"event timestamp {entry.event.timestamp_ns} is "
                        f"older than last-consumed {self._last_consumed_ns}; "
                        f"kind={entry.event.kind}"
                    )
                self._last_consumed_ns = entry.event.timestamp_ns
        self._finished.release()
        self._unfinished_tasks -= 1

        try:
            if self._enforce_temporal_order:
                with self._lock:
                    if (
                        self._last_consumed_ns is not None
                        and entry.event.timestamp_ns < self._last_consumed_ns
                    ):
                        raise TemporalSequenceViolationError(
                            f"event timestamp {entry.event.timestamp_ns} is "
                            f"older than last-consumed {self._last_consumed_ns}; "
                            f"kind={entry.event.kind}"
                        )
                    self._last_consumed_ns = entry.event.timestamp_ns
            return entry.event
        finally:
            self._finished.release()
            self._unfinished_tasks -= 1

    def get_nowait(self) -> Event:
        """Non-blocking pop. Raises ``queue.Empty`` if empty."""
        return self.get(timeout=0)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def poison(self) -> None:
        """Mark the queue as poisoned. All blocked ``get`` calls will
        wake with :class:`QueuePoisonedError`.
        """
        self._poisoned.set()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Block until every enqueued event has been consumed.

        Returns ``True`` on completion, ``False`` on timeout.
        """
        result = self._finished.acquire(timeout=timeout)
        # Drain any remaining acquires so the semaphore is balanced.
        while True:
            if not self._finished.acquire(timeout=0):
                break
            result = True
        return result

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def qsize(self) -> int:
        """Approximate number of events currently queued."""
        return self._heap.qsize()

    def __len__(self) -> int:
        return self.qsize()

    def empty(self) -> bool:
        return self.qsize() == 0

    @property
    def last_consumed_ns(self) -> Optional[int]:
        """Timestamp of the most recently consumed event, if any."""
        return self._last_consumed_ns

    @property
    def is_poisoned(self) -> bool:
        return self._poisoned.is_set()

    @property
    def known_order_ids(self) -> frozenset[str]:
        """Snapshot of order_ids currently accepted by the queue."""
        with self._lock:
            return frozenset(self._known_order_ids)


def _monotonic() -> float:
    """Local import shim so the module top stays stdlib-only."""
    return threading.current_thread().native_id and __import__("time").monotonic()


__all__ = ["EventQueue"]
