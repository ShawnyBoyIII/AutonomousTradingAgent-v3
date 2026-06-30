"""Tests for the message bus (pub/sub event routing)."""

from __future__ import annotations

import pytest

from trading_bot.events.bus import MessageBus
from trading_bot.events.types import (
    Event,
    MarketTickEvent,
    OrderFillEvent,
    PortfolioStateEvent,
    StrategySignalEvent,
    SystemTickEvent,
)


class TestMessageBusBasic:
    """Core message bus functionality."""

    def test_publish_and_subscriber_receives(self):
        bus = MessageBus()
        received = []
        bus.subscribe("MARKET_TICK", lambda e: received.append(e))
        bus.publish(MarketTickEvent(ticker="AAPL", price=150.0))
        assert len(received) == 1
        assert received[0].ticker == "AAPL"
        assert received[0].price == 150.0

    def test_multiple_subscribers_all_called(self):
        bus = MessageBus()
        received_a = []
        received_b = []
        bus.subscribe("MARKET_TICK", lambda e: received_a.append(e))
        bus.subscribe("MARKET_TICK", lambda e: received_b.append(e))
        bus.publish(MarketTickEvent(ticker="AAPL", price=150.0))
        assert len(received_a) == 1
        assert len(received_b) == 1

    def test_subscribers_called_in_order(self):
        bus = MessageBus()
        order = []
        bus.subscribe("MARKET_TICK", lambda e: order.append("first"))
        bus.subscribe("MARKET_TICK", lambda e: order.append("second"))
        bus.subscribe("MARKET_TICK", lambda e: order.append("third"))
        bus.publish(MarketTickEvent())
        assert order == ["first", "second", "third"]

    def test_unsubscribe_removes_handler(self):
        bus = MessageBus()
        received = []
        handler = lambda e: received.append(e)
        bus.subscribe("MARKET_TICK", handler)
        bus.publish(MarketTickEvent(ticker="AAPL"))
        assert len(received) == 1
        bus.unsubscribe("MARKET_TICK", handler)
        bus.publish(MarketTickEvent(ticker="AAPL"))
        assert len(received) == 1

    def test_unsubscribe_returns_false_for_unknown_handler(self):
        bus = MessageBus()
        handler = lambda e: None
        assert bus.unsubscribe("MARKET_TICK", handler) is False

    def test_publish_to_exact_topic(self):
        bus = MessageBus()
        received = []
        bus.subscribe("MARKET_TICK", lambda e: received.append(e))
        bus.publish_to("MARKET_TICK", MarketTickEvent(ticker="AAPL"))
        assert len(received) == 1

    def test_publish_to_wrong_topic_delivers_nothing(self):
        bus = MessageBus()
        received = []
        bus.subscribe("MARKET_TICK", lambda e: received.append(e))
        bus.publish_to("ORDER_FILL", OrderFillEvent())
        assert len(received) == 0


class TestWildcardRouting:
    """Topic-based wildcard routing."""

    def test_star_wildcard_matches_all(self):
        bus = MessageBus()
        received = []
        bus.subscribe("*", lambda e: received.append(e.event_type))
        bus.publish(MarketTickEvent(ticker="AAPL"))
        bus.publish(StrategySignalEvent(ticker="SPY"))
        assert received == ["MARKET_TICK", "STRATEGY_SIGNAL"]

    def test_prefix_wildcard_matches_subtopics(self):
        bus = MessageBus()
        received = []
        bus.subscribe("ORDER*", lambda e: received.append(e.event_type))
        bus.publish(OrderFillEvent(ticker="AAPL"))
        bus.publish(OrderFillEvent(ticker="SPY"))
        bus.publish(MarketTickEvent(ticker="AAPL"))
        assert received == ["ORDER_FILL", "ORDER_FILL"]
        assert len(received) == 2

    def test_exact_topic_match(self):
        bus = MessageBus()
        received = []
        bus.subscribe("MARKET_TICK", lambda e: received.append(e.event_type))
        bus.publish(MarketTickEvent(ticker="AAPL"))
        bus.publish(MarketTickEvent(ticker="SPY"))
        assert len(received) == 2

    def test_event_type_exact_match(self):
        bus = MessageBus()
        received = []
        bus.subscribe("MARKET_TICK", lambda e: received.append(e.event_type))
        bus.publish(MarketTickEvent(ticker="AAPL"))
        assert received == ["MARKET_TICK"]


class TestEventLogging:
    """Event log management."""

    def test_events_are_logged(self):
        bus = MessageBus()
        bus.publish(MarketTickEvent(ticker="AAPL"))
        bus.publish(MarketTickEvent(ticker="SPY"))
        assert bus.log_size == 2

    def test_get_recent_returns_last_n(self):
        bus = MessageBus()
        for i in range(10):
            bus.publish(MarketTickEvent(ticker=f"SYM{i}"))
        recent = bus.get_recent(5)
        assert len(recent) == 5
        assert recent[0].ticker == "SYM5"
        assert recent[-1].ticker == "SYM9"

    def test_clear_log_removes_all_events(self):
        bus = MessageBus()
        bus.publish(MarketTickEvent(ticker="AAPL"))
        bus.publish(MarketTickEvent(ticker="SPY"))
        assert bus.log_size == 2
        bus.clear_log()
        assert bus.log_size == 0

    def test_log_truncates_at_max_size(self):
        bus = MessageBus()
        bus._max_log_size = 100
        for i in range(150):
            bus.publish(MarketTickEvent(ticker=f"SYM{i}"))
        # After truncation to 50, we add 50 more = 100, but 101st triggers truncation
        # So after 150 events: 50 (after first truncation) + 49 = 99
        assert bus.log_size == 99

    def test_log_truncates_to_half_then_grows(self):
        bus = MessageBus()
        bus._max_log_size = 100
        for i in range(100):
            bus.publish(MarketTickEvent(ticker=f"SYM{i}"))
        assert bus.log_size == 100
        for i in range(100, 150):
            bus.publish(MarketTickEvent(ticker=f"SYM{i}"))
        # After 150 total: 50 (after truncation) + 49 = 99
        assert bus.log_size == 99
        assert bus.get_recent(1)[0].ticker == "SYM149"


class TestHandlerErrors:
    """Handler exception handling."""

    def test_handler_exception_does_not_crash_bus(self):
        bus = MessageBus()
        received = []

        def bad_handler(e):
            raise ValueError("boom")

        def good_handler(e):
            received.append(e)

        bus.subscribe("MARKET_TICK", bad_handler)
        bus.subscribe("MARKET_TICK", good_handler)
        bus.publish(MarketTickEvent(ticker="AAPL"))
        assert len(received) == 1

    def test_handler_exception_does_not_affect_later_handlers(self):
        bus = MessageBus()
        received = []

        def first_handler(e):
            received.append("first")

        def bad_handler(e):
            raise ValueError("boom")

        def second_handler(e):
            received.append("second")

        bus.subscribe("MARKET_TICK", first_handler)
        bus.subscribe("MARKET_TICK", bad_handler)
        bus.subscribe("MARKET_TICK", second_handler)
        bus.publish(MarketTickEvent())
        assert received == ["first", "second"]

    def test_publish_to_handler_exception(self):
        bus = MessageBus()
        received = []

        def bad_handler(e):
            raise ValueError("boom")

        def good_handler(e):
            received.append(e)

        bus.subscribe("MARKET_TICK", bad_handler)
        bus.subscribe("MARKET_TICK", good_handler)
        bus.publish_to("MARKET_TICK", MarketTickEvent(ticker="AAPL"))
        assert len(received) == 1
