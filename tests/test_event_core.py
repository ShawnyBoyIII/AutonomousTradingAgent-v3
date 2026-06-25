"""Tests for the event-driven core: types, bus, loop, and cache."""

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from trading_bot.events.bus import MessageBus
from trading_bot.events.cache import Cache
from trading_bot.events.loop import EventLoop
from trading_bot.events.types import (
    MarketBarEvent,
    OrderFillEvent,
    PortfolioStateEvent,
    StrategySignalEvent,
    SystemTickEvent,
)


class TestEventType:
    def test_event_instantiation(self):
        event = SystemTickEvent(tick=42)
        assert event.event_type == "SYSTEM_TICK"
        assert event.tick == 42
        assert event.timestamp.tzinfo is not None

    def test_event_sorting(self):
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        e1 = SystemTickEvent(tick=1, timestamp=t1)
        e2 = SystemTickEvent(tick=2, timestamp=t2)
        assert e1 < e2

    def test_event_comparison_non_event(self):
        e = SystemTickEvent()
        assert e.__lt__(42) is NotImplemented


class TestMessageBus:
    def test_subscribe_and_publish(self):
        bus = MessageBus()
        handler = Mock()
        bus.subscribe("MARKET_BAR", handler)
        event = MarketBarEvent(ticker="AAPL", close=150.0)
        bus.publish(event)
        handler.assert_called_once_with(event)

    def test_wildcard_subscription(self):
        bus = MessageBus()
        handler = Mock()
        bus.subscribe("MARKET_*", handler)
        bus.publish(MarketBarEvent(ticker="AAPL"))
        bus.publish(MarketBarEvent(ticker="SPY"))
        assert handler.call_count == 2

    def test_global_wildcard(self):
        bus = MessageBus()
        handler = Mock()
        bus.subscribe("*", handler)
        bus.publish(MarketBarEvent(ticker="AAPL"))
        bus.publish(StrategySignalEvent(ticker="SPY"))
        assert handler.call_count == 2

    def test_unsubscribe(self):
        bus = MessageBus()
        handler = Mock()
        bus.subscribe("MARKET_BAR", handler)
        bus.unsubscribe("MARKET_BAR", handler)
        bus.publish(MarketBarEvent(ticker="AAPL"))
        handler.assert_not_called()

    def test_unsubscribe_nonexistent(self):
        bus = MessageBus()
        handler = Mock()
        result = bus.unsubscribe("MARKET_BAR", handler)
        assert result is False

    def test_event_log(self):
        bus = MessageBus()
        bus.subscribe("MARKET_BAR", Mock())
        bus.publish(MarketBarEvent(ticker="AAPL"))
        bus.publish(MarketBarEvent(ticker="SPY"))
        assert bus.log_size == 2
        recent = bus.get_recent(1)
        assert len(recent) == 1
        assert recent[0].ticker == "SPY"

    def test_clear_log(self):
        bus = MessageBus()
        bus.subscribe("MARKET_BAR", Mock())
        bus.publish(MarketBarEvent(ticker="AAPL"))
        bus.clear_log()
        assert bus.log_size == 0

    def test_handler_exception_doesnt_crash(self):
        bus = MessageBus()
        bad_handler = Mock(side_effect=ValueError("boom"))
        good_handler = Mock()
        bus.subscribe("MARKET_BAR", bad_handler)
        bus.subscribe("MARKET_BAR", good_handler)
        bus.publish(MarketBarEvent(ticker="AAPL"))
        good_handler.assert_called_once()

    def test_max_log_size(self):
        bus = MessageBus()
        bus._max_log_size = 10
        bus.subscribe("MARKET_BAR", Mock())
        for i in range(15):
            bus.publish(MarketBarEvent(ticker="AAPL"))
        assert bus.log_size <= 10


class TestEventLoop:
    def test_submit_and_step(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        handler = Mock()
        loop.register_handler("MARKET_BAR", handler)
        loop.submit(MarketBarEvent(ticker="AAPL"))
        result = loop.step()
        assert result is True
        handler.assert_called_once()

    def test_run_with_max_events(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        loop.submit(MarketBarEvent(ticker="AAPL"))
        loop.submit(MarketBarEvent(ticker="SPY"))
        processed = loop.run(max_events=1)
        assert processed == 1

    def test_run_empty_queue(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        processed = loop.run()
        assert processed == 0

    def test_queue_overflow(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        loop._max_queue_size = 5
        for i in range(10):
            loop.submit(MarketBarEvent(ticker="AAPL"))
        assert loop.queue_depth == 5

    def test_stop(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        loop.submit(MarketBarEvent(ticker="AAPL"))
        loop.submit(MarketBarEvent(ticker="SPY"))
        loop._running = True
        loop.run(max_events=1)
        assert loop.is_running is False

    def test_events_processed_counter(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        loop.submit(MarketBarEvent(ticker="AAPL"))
        loop.submit(MarketBarEvent(ticker="SPY"))
        loop.run(max_events=2)
        assert loop.events_processed == 2

    def test_clear(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        loop.submit(MarketBarEvent(ticker="AAPL"))
        loop.clear()
        assert loop.queue_depth == 0

    def test_run_until_empty(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        loop.submit(MarketBarEvent(ticker="AAPL"))
        loop.submit(MarketBarEvent(ticker="SPY"))
        processed = loop.run_until_empty()
        assert processed == 2

    def test_unregister_handler(self):
        bus = MessageBus()
        loop = EventLoop(bus)
        handler = Mock()
        loop.register_handler("MARKET_BAR", handler)
        loop.unregister_handler("MARKET_BAR", handler)
        loop.submit(MarketBarEvent(ticker="AAPL"))
        loop.run()
        handler.assert_not_called()


class TestCache:
    def test_initial_state(self):
        cache = Cache()
        assert cache.cash == 100_000.0
        assert cache.equity == 100_000.0
        assert cache.get_open_positions() == []
        assert cache.get_exposure_ratio() == 0.0

    def test_update_from_state_event(self):
        cache = Cache()
        event = PortfolioStateEvent(
            cash=90_000.0,
            equity=110_000.0,
            positions={"AAPL": {"quantity": 10, "average_cost": 150.0}},
            realized_pnl=500.0,
        )
        cache.update_from_state_event(event)
        assert cache.cash == 90_000.0
        assert cache.equity == 110_000.0
        assert cache.realized_pnl == 500.0
        assert "AAPL" in cache.get_open_positions()

    def test_update_from_fill(self):
        cache = Cache()
        event = OrderFillEvent(
            order_id="o1",
            ticker="AAPL",
            quantity=10,
            fill_price=150.0,
            fees=1.0,
            side="BUY",
        )
        cache.update_from_fill(event)
        pos = cache.get_position("AAPL")
        assert pos is not None
        assert pos["quantity"] == 10
        assert pos["average_cost"] == 150.0

    def test_fill_updates_average_cost(self):
        cache = Cache()
        cache.update_from_fill(OrderFillEvent(
            order_id="o1", ticker="AAPL", quantity=10,
            fill_price=100.0, fees=0.0, side="BUY",
        ))
        cache.update_from_fill(OrderFillEvent(
            order_id="o2", ticker="AAPL", quantity=10,
            fill_price=200.0, fees=0.0, side="BUY",
        ))
        pos = cache.get_position("AAPL")
        assert pos["quantity"] == 20
        assert pos["average_cost"] == 150.0

    def test_get_exposure(self):
        cache = Cache()
        cache.cash = 90_000.0
        cache.equity = 110_000.0
        cache.positions["AAPL"] = {"quantity": 100, "average_cost": 150.0}
        assert cache.get_exposure() == 15_000.0
        assert cache.get_exposure_ratio() == pytest.approx(15_000 / 110_000)

    def test_get_recent_bars(self):
        cache = Cache()
        for i in range(5):
            cache.update_from_bar(MarketBarEvent(
                ticker="AAPL", close=float(100 + i), timeframe="1m",
            ))
        bars = cache.get_recent_bars("AAPL", n=3)
        assert len(bars) == 3

    def test_reset(self):
        cache = Cache()
        cache.update_from_state_event(PortfolioStateEvent(
            cash=90_000.0, equity=110_000.0,
            positions={"AAPL": {"quantity": 10}},
        ))
        cache.reset()
        assert cache.cash == 100_000.0
        assert cache.positions == {}

    def test_signal_history(self):
        cache = Cache()
        cache.update_from_signal(StrategySignalEvent(
            ticker="AAPL", action="BUY", confidence=0.8,
        ))
        signals = cache.get_recent_signals("AAPL")
        assert len(signals) == 1
        assert signals[0]["action"] == "BUY"

    def test_fill_history_filtering(self):
        cache = Cache()
        cache.update_from_fill(OrderFillEvent(
            order_id="o1", ticker="AAPL", quantity=10,
            fill_price=150.0, fees=0.0, side="BUY",
        ))
        cache.update_from_fill(OrderFillEvent(
            order_id="o2", ticker="SPY", quantity=5,
            fill_price=400.0, fees=0.0, side="BUY",
        ))
        aapl_fills = cache.get_fill_history("AAPL")
        assert len(aapl_fills) == 1
        assert aapl_fills[0]["ticker"] == "AAPL"
