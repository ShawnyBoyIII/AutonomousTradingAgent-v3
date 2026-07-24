"""Stage 1: event data classes — immutability, validation, defaults."""
from __future__ import annotations

import dataclasses
import time

import pytest

from event_engine.events import (
    BarType,
    FillEvent,
    MarketEvent,
    OrderDirection,
    OrderEvent,
    OrderType,
    SignalDirection,
    SignalEvent,
    TimeInForce,
)
from event_engine.exceptions import EventValidationError


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


def test_event_base_has_kind_class_var_override_pattern():
    """The abstract ``Event`` exposes ``kind`` as a ClassVar that
    concrete subclasses override. This documents the polymorphic
    discriminator pattern without depending on Python's ABC-meta
    abstract-instantiation guard (which does not interact with
    dataclass on Python 3.13+)."""
    from event_engine.events import Event  # local import to avoid module shadowing

    assert Event.kind == "ABSTRACT"
    # All concrete subclasses must override ``kind``.
    assert MarketEvent.kind == "MARKET"
    assert SignalEvent.kind == "SIGNAL"
    assert OrderEvent.kind == "ORDER"
    assert FillEvent.kind == "FILL"


def test_all_concrete_events_have_kind_class_attr():
    assert MarketEvent.kind == "MARKET"
    assert SignalEvent.kind == "SIGNAL"
    assert OrderEvent.kind == "ORDER"
    assert FillEvent.kind == "FILL"


# ---------------------------------------------------------------------------
# MarketEvent
# ---------------------------------------------------------------------------


def _market_event(**overrides) -> MarketEvent:
    base = dict(
        timestamp_ns=1_700_000_000_000_000_000,
        symbol="AAPL",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1_000.0,
        bid_ask_spread=0.01,
        bar_type=BarType.BAR_1M,
    )
    base.update(overrides)
    return MarketEvent(**base)


def test_market_event_constructs_with_required_fields():
    e = _market_event()
    assert e.symbol == "AAPL"
    assert e.open == 100.0
    assert e.high == 101.0
    assert e.low == 99.0
    assert e.close == 100.5
    assert e.volume == 1_000.0
    assert e.bid_ask_spread == 0.01
    assert e.bar_type == BarType.BAR_1M


def test_market_event_rejects_nonempty_symbol():
    with pytest.raises(EventValidationError):
        _market_event(symbol="")


def test_market_event_rejects_ohlc_incoherence():
    with pytest.raises(EventValidationError):
        _market_event(high=98.0, open=100.0, low=99.0)
    with pytest.raises(EventValidationError):
        _market_event(low=110.0)
    with pytest.raises(EventValidationError):
        _market_event(open=200.0, high=150.0)


def test_market_event_rejects_negative_volume():
    with pytest.raises(EventValidationError):
        _market_event(volume=-1.0)


def test_market_event_rejects_nan_in_price():
    with pytest.raises(EventValidationError):
        _market_event(open=float("nan"))
    with pytest.raises(EventValidationError):
        _market_event(close=float("inf"))


# ---------------------------------------------------------------------------
# SignalEvent
# ---------------------------------------------------------------------------


def test_signal_event_constructs_long_short_exit():
    base = dict(timestamp_ns=1_700_000_000_000_000_000, symbol="AAPL",
                target_quantity=10)
    long = SignalEvent(signal_type=SignalDirection.LONG, strength=0.7, **base)
    short = SignalEvent(signal_type=SignalDirection.SHORT, strength=-0.7, **base)
    exit_signal = SignalEvent(
        timestamp_ns=1_700_000_000_000_000_000, symbol="AAPL",
        signal_type=SignalDirection.EXIT, strength=0.0, target_quantity=0,
    )
    assert long.signal_type is SignalDirection.LONG
    assert short.signal_type is SignalDirection.SHORT
    assert exit_signal.signal_type is SignalDirection.EXIT


def test_signal_event_rejects_strength_outside_unit_interval():
    base = dict(timestamp_ns=1_700_000_000_000_000_000, symbol="AAPL",
                target_quantity=10)
    with pytest.raises(EventValidationError):
        SignalEvent(signal_type=SignalDirection.LONG, strength=1.5, **base)
    with pytest.raises(EventValidationError):
        SignalEvent(signal_type=SignalDirection.LONG, strength=-1.5, **base)


def test_signal_event_rejects_negative_target_quantity():
    with pytest.raises(EventValidationError):
        SignalEvent(
            timestamp_ns=1_700_000_000_000_000_000,
            symbol="AAPL",
            signal_type=SignalDirection.LONG,
            strength=0.5,
            target_quantity=-5,
        )


# ---------------------------------------------------------------------------
# OrderEvent
# ---------------------------------------------------------------------------


def _order_event(**overrides) -> OrderEvent:
    base = dict(
        timestamp_ns=1_700_000_000_000_000_000,
        symbol="AAPL",
        order_type=OrderType.MARKET,
        direction=OrderDirection.BUY,
        quantity=10,
        order_id="O-AAPL-1",
    )
    base.update(overrides)
    return OrderEvent(**base)


def test_order_event_market_has_no_required_prices():
    o = _order_event()
    assert o.order_type is OrderType.MARKET
    assert o.limit_price is None
    assert o.stop_price is None
    assert o.time_in_force is TimeInForce.GTC


def test_order_event_limit_requires_limit_price():
    with pytest.raises(EventValidationError):
        _order_event(order_type=OrderType.LIMIT, limit_price=None)


def test_order_event_stop_requires_stop_price():
    with pytest.raises(EventValidationError):
        _order_event(order_type=OrderType.STOP, stop_price=None)


def test_order_event_iceberg_allowed_without_prices():
    o = _order_event(order_type=OrderType.ICEBERG)
    assert o.order_type is OrderType.ICEBERG


def test_order_event_rejects_zero_quantity():
    with pytest.raises(EventValidationError):
        _order_event(quantity=0)


def test_order_event_rejects_empty_order_id():
    with pytest.raises(EventValidationError):
        _order_event(order_id="")


# ---------------------------------------------------------------------------
# FillEvent
# ---------------------------------------------------------------------------


def _fill_event(**overrides) -> FillEvent:
    base = dict(
        timestamp_ns=1_700_000_000_000_000_000,
        symbol="AAPL",
        exchange="SIM",
        quantity_filled=10,
        fill_price=100.0,
        direction=OrderDirection.BUY,
        commission_fee=1.0,
        slippage_cost=0.0,
        impact_cost=0.0,
        order_id="O-AAPL-1",
    )
    base.update(overrides)
    return FillEvent(**base)


def test_fill_event_constructs_with_required_fields():
    f = _fill_event()
    assert f.exchange == "SIM"
    assert f.commission_fee == 1.0
    assert f.impact_cost == 0.0


def test_fill_event_rejects_negative_quantity_filled():
    with pytest.raises(EventValidationError):
        _fill_event(quantity_filled=-1)


def test_fill_event_rejects_negative_fees():
    with pytest.raises(EventValidationError):
        _fill_event(commission_fee=-0.01)


def test_fill_event_allows_zero_partial_fills():
    f = _fill_event(quantity_filled=0)
    assert f.quantity_filled == 0


# ---------------------------------------------------------------------------
# Immutability (frozen)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_factory",
    [
        lambda: _market_event(),
        lambda: _order_event(),
        lambda: _fill_event(),
    ],
)
def test_events_are_frozen(event_factory):
    """``frozen=True`` is non-negotiable for thread-safety."""
    e = event_factory()
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.symbol = "MUTATED"


def test_slots_layout():
    """``slots=True`` precludes ``__dict__`` — verified via the
    ``__slots__`` dataclass field tuple."""
    e = _market_event()
    slots = e.__slots__ if hasattr(e, "__slots__") else ()
    assert "symbol" in slots


def test_hash_is_immutable_per_event_content():
    """Two MarketEvents with identical content have equal hashes."""
    a = _market_event()
    b = _market_event()
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# Timestamp validation
# ---------------------------------------------------------------------------


def test_events_reject_non_int_timestamp():
    for factory in (
        lambda: _market_event(timestamp_ns=1.5),
        lambda: _order_event(timestamp_ns=1.5),
        lambda: _fill_event(timestamp_ns=1.5),
    ):
        with pytest.raises(EventValidationError):
            factory()


def test_events_reject_negative_timestamp():
    with pytest.raises(EventValidationError):
        _market_event(timestamp_ns=-1)


def test_events_reject_year_10000_timestamp():
    """Out-of-range timestamps above year 9999 raise."""
    with pytest.raises(EventValidationError):
        _market_event(timestamp_ns=10**22)
