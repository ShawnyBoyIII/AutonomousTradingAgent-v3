"""Stage 1: EventQueue — ordering, validation, dedupe, exceptions."""
from __future__ import annotations

import queue as stdqueue
import threading
import time

import pytest

from event_engine.events import (
    MarketEvent,
    BarType,
    OrderEvent,
    OrderDirection,
    OrderType,
    TimeInForce,
)
from event_engine.exceptions import (
    DuplicateOrderIdError,
    EventValidationError,
    QueuePoisonedError,
    QueueStarvationError,
    TemporalSequenceViolationError,
)
from event_engine.queue import EventQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _market(symbol: str, ts_offset: int, *, base_ts: int) -> MarketEvent:
    return MarketEvent(
        timestamp_ns=base_ts + ts_offset,
        symbol=symbol,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000.0,
        bid_ask_spread=0.01,
        bar_type=BarType.BAR_1M,
    )


def _order(order_id: str, ts_offset: int, *, base_ts: int, qty: int = 10) -> OrderEvent:
    return OrderEvent(
        timestamp_ns=base_ts + ts_offset,
        symbol="AAPL",
        order_type=OrderType.MARKET,
        direction=OrderDirection.BUY,
        quantity=qty,
        order_id=order_id,
        time_in_force=TimeInForce.GTC,
    )


# ---------------------------------------------------------------------------
# Ordering / temporal validation
# ---------------------------------------------------------------------------


def test_put_returns_in_timestamp_ascending_order(base_ts_ns):
    q = EventQueue()
    q.put(_market("AAPL", 20, base_ts=base_ts_ns))
    q.put(_market("MSFT", 5, base_ts=base_ts_ns))
    q.put(_market("AAPL", 30, base_ts=base_ts_ns))

    pops = [q.get().timestamp_ns for _ in range(3)]
    assert pops == sorted(pops)


def test_out_of_order_consumption_raises(base_ts_ns):
    q = EventQueue()
    q.put(_market("A", 100, base_ts=base_ts_ns))
    q.put(_market("B", 200, base_ts=base_ts_ns))
    q.get()  # drains the future

    older = _market("C", 50, base_ts=base_ts_ns)
    q.put(older)

    with pytest.raises(TemporalSequenceViolationError):
        q.get()


def test_equal_timestamps_break_tie_by_put_order(base_ts_ns):
    """Two events at the *same* timestamp resolve by seqno so the
    queue is total-ordered even when timestamps collide."""
    q = EventQueue()
    e1 = _market("A", 0, base_ts=base_ts_ns)
    e2 = _market("B", 0, base_ts=base_ts_ns)
    q.put(e1)
    q.put(e2)
    assert q.get().symbol == "A"
    assert q.get().symbol == "B"


def test_get_then_get_returns_minimum_available(base_ts_ns):
    q = EventQueue()
    for offset in (50, 20, 30, 10, 40):
        q.put(_market(f"T{offset}", offset, base_ts=base_ts_ns))
    pops = [q.get().timestamp_ns for _ in range(5)]
    assert pops[0] < pops[-1]


# ---------------------------------------------------------------------------
# Order-id dedupe
# ---------------------------------------------------------------------------


def test_same_order_id_second_put_raises(base_ts_ns):
    q = EventQueue()
    q.put(_order("X", 10, base_ts=base_ts_ns))
    q.get()
    with pytest.raises(DuplicateOrderIdError):
        q.put(_order("X", 20, base_ts=base_ts_ns))


def test_same_order_id_rejected_even_before_first_get(base_ts_ns):
    """Dedupe is independent of consumption order."""
    q = EventQueue()
    q.put(_order("X", 10, base_ts=base_ts_ns))
    with pytest.raises(DuplicateOrderIdError):
        q.put(_order("X", 30, base_ts=base_ts_ns))


def test_distinct_order_ids_accepted(base_ts_ns):
    q = EventQueue()
    q.put(_order("X", 10, base_ts=base_ts_ns))
    q.put(_order("Y", 20, base_ts=base_ts_ns))
    q.put(_order("Z", 30, base_ts=base_ts_ns))
    assert q.qsize() == 3


# ---------------------------------------------------------------------------
# Exceptions and lifecycle
# ---------------------------------------------------------------------------


def test_poisoned_put_raises(base_ts_ns):
    q = EventQueue()
    q.poison()
    with pytest.raises(QueuePoisonedError):
        q.put(_market("A", 1, base_ts=base_ts_ns))


def test_poisoned_get_raises_even_if_nonempty(base_ts_ns):
    q = EventQueue()
    q.put(_market("A", 1, base_ts=base_ts_ns))
    q.poison()
    with pytest.raises(QueuePoisonedError):
        q.get()


def test_starvation_raises_with_timeout(base_ts_ns):
    q = EventQueue()
    t0 = time.monotonic()
    with pytest.raises(QueueStarvationError):
        q.get(timeout=0.05)
    assert time.monotonic() - t0 < 0.5


def test_get_nowait_returns_immediately_when_empty():
    q = EventQueue()
    with pytest.raises(QueueStarvationError):
        q.get_nowait()


def test_maxsize_zero_is_unbounded(base_ts_ns):
    q = EventQueue(maxsize=0)
    for i in range(100):
        q.put(_market(f"T{i}", i, base_ts=base_ts_ns))
    assert q.qsize() == 100


def test_maxsize_enforces_backpressure(base_ts_ns):
    q = EventQueue(maxsize=2)
    q.put(_market("A", 0, base_ts=base_ts_ns))
    q.put(_market("B", 1, base_ts=base_ts_ns))
    with pytest.raises(stdqueue.Full):
        q.put_nowait(_market("C", 2, base_ts=base_ts_ns))


def test_put_rejects_non_event():
    q = EventQueue()
    with pytest.raises(EventValidationError):
        q.put({"not": "an event"})


# ---------------------------------------------------------------------------
# Thread safety smoke test
# ---------------------------------------------------------------------------


def test_concurrent_producer_consumer_does_not_deadlock(base_ts_ns):
    """50 producers push 50 events each; a single consumer drains.
    This is a smoke test, not a Torture-suite verification."""
    q = EventQueue()
    produced = []
    produced_lock = threading.Lock()

    def producer(idx: int):
        for j in range(50):
            e = _market(f"P{idx}-T{j}", idx * 100 + j, base_ts=base_ts_ns)
            q.put(e)
        with produced_lock:
            produced.append(idx)

    threads = [threading.Thread(target=producer, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()

    consumed = 0
    deadline = time.monotonic() + 5.0
    consumed_unique = set()
    while consumed < 2500 and time.monotonic() < deadline:
        try:
            ev = q.get(timeout=0.1)
            consumed_unique.add(ev.symbol.split("-")[0])
            consumed += 1
        except QueueStarvationError:
            pass

    for t in threads:
        t.join(timeout=2.0)

    assert consumed == 2500, f"expected 2500 events, got {consumed}"
    assert len(produced) == 50
    assert len(consumed_unique) == 50


# ---------------------------------------------------------------------------
# Snapshot properties
# ---------------------------------------------------------------------------


def test_last_consumed_ns_is_none_until_first_get(base_ts_ns):
    q = EventQueue()
    assert q.last_consumed_ns is None
    q.put(_market("A", 10, base_ts=base_ts_ns))
    assert q.last_consumed_ns is None
    q.get()
    assert q.last_consumed_ns == base_ts_ns + 10


def test_known_order_ids_returns_frozenset(base_ts_ns):
    q = EventQueue()
    q.put(_order("X", 10, base_ts=base_ts_ns))
    q.put(_order("Y", 20, base_ts=base_ts_ns))
    snap = q.known_order_ids
    assert isinstance(snap, frozenset)
    assert "X" in snap and "Y" in snap
