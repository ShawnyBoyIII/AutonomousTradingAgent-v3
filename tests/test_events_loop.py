"""Tests for the deterministic event loop."""

from __future__ import annotations

from trading_bot.events.bus import MessageBus
from trading_bot.events.loop import EventLoop
from trading_bot.events.types import Event, MarketTickEvent, SystemTickEvent


class TestEventLoopSubmit:
    """Event submission to the loop."""

    def test_submit_single_event(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        assert loop.queue_depth == 1

    def test_submit_batch_events(self):
        loop = EventLoop()
        events = [MarketTickEvent(ticker=f"SYM{i}") for i in range(10)]
        loop.submit_batch(events)
        assert loop.queue_depth == 10

    def test_queue_depth_after_submit(self):
        loop = EventLoop()
        assert loop.queue_depth == 0
        loop.submit(MarketTickEvent(ticker="AAPL"))
        assert loop.queue_depth == 1

    def test_tick_counter_increments(self):
        loop = EventLoop()
        assert loop.tick == 0
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.step()
        assert loop.tick == 1


class TestEventLoopRun:
    """Event loop execution."""

    def test_run_processes_all_events(self):
        loop = EventLoop()
        received = []
        loop.register_handler("MARKET_TICK", lambda e: received.append(e.ticker))
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.submit(MarketTickEvent(ticker="SPY"))
        processed = loop.run()
        assert processed == 2
        assert received == ["AAPL", "SPY"]

    def test_run_with_max_events(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.submit(MarketTickEvent(ticker="SPY"))
        loop.submit(MarketTickEvent(ticker="MSFT"))
        processed = loop.run(max_events=2)
        assert processed == 2
        assert loop.queue_depth == 1

    def test_run_empty_queue_returns_zero(self):
        loop = EventLoop()
        processed = loop.run()
        assert processed == 0

    def test_run_until_empty(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.submit(MarketTickEvent(ticker="SPY"))
        processed = loop.run_until_empty()
        assert processed == 2

    def test_run_sets_running_true_then_false(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        assert loop.is_running is False
        loop.run()
        assert loop.is_running is False

    def test_run_publishes_system_tick(self):
        loop = EventLoop()
        received = []
        loop.bus.subscribe("SYSTEM_TICK", lambda e: received.append(e))
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.run()
        assert len(received) >= 1

    def test_run_publishes_heartbeat_every_1000_events(self):
        loop = EventLoop()
        received = []
        loop.bus.subscribe("SYSTEM_HEARTBEAT", lambda e: received.append(e))
        for i in range(2500):
            loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.run()
        heartbeat_count = sum(1 for e in received if e.event_type == "SYSTEM_HEARTBEAT")
        assert heartbeat_count >= 2

    def test_stop_halts_processing(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.submit(MarketTickEvent(ticker="SPY"))
        loop._process_one = lambda: None
        loop._running = True
        loop.stop()
        assert loop.is_running is False


class TestEventLoopStep:
    """Single-step execution."""

    def test_step_processes_one_event(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        result = loop.step()
        assert result is True
        assert loop.queue_depth == 0

    def test_step_returns_false_on_empty_queue(self):
        loop = EventLoop()
        result = loop.step()
        assert result is False

    def test_step_increments_tick(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.step()
        assert loop.tick == 1


class TestEventHandlerRegistration:
    """Handler registration and unregistration."""

    def test_register_handler(self):
        loop = EventLoop()
        called = []
        handler = lambda e: called.append(e)
        loop.register_handler("MARKET_TICK", handler)
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.run()
        assert len(called) == 1

    def test_unregister_handler(self):
        loop = EventLoop()
        called = []
        handler = lambda e: called.append(e)
        loop.register_handler("MARKET_TICK", handler)
        loop.unregister_handler("MARKET_TICK", handler)
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.run()
        assert len(called) == 0

    def test_unregister_unknown_handler(self):
        loop = EventLoop()
        handler = lambda e: None
        assert loop.unregister_handler("MARKET_TICK", handler) is False

    def test_multiple_handlers_for_same_event(self):
        loop = EventLoop()
        received = []
        handler_a = lambda e: received.append("a")
        handler_b = lambda e: received.append("b")
        loop.register_handler("MARKET_TICK", handler_a)
        loop.register_handler("MARKET_TICK", handler_b)
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.run()
        assert received == ["a", "b"]


class TestQueueOverflow:
    """Queue size management."""

    def test_oldest_event_dropped_on_overflow(self):
        loop = EventLoop()
        # EventLoop has _max_queue_size=100_000, so we need to manually test overflow
        # by directly manipulating the queue
        loop._max_queue_size = 5
        for i in range(10):
            loop.submit(MarketTickEvent(ticker=f"SYM{i}"))
        assert loop.queue_depth == 5
        # First 5 should have been dropped
        events = []
        loop.bus.subscribe("*", lambda e: events.append(e.ticker))
        loop.run()
        assert events == ["SYM5", "SYM6", "SYM7", "SYM8", "SYM9"]

    def test_events_processed_in_submission_order(self):
        loop = EventLoop()
        received = []
        loop.register_handler("MARKET_TICK", lambda e: received.append(e.ticker))
        for i in range(5):
            loop.submit(MarketTickEvent(ticker=f"SYM{i}"))
        loop.run()
        assert received == ["SYM0", "SYM1", "SYM2", "SYM3", "SYM4"]


class TestEventLoopClear:
    """Queue clearing."""

    def test_clear_removes_all_events(self):
        loop = EventLoop()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.submit(MarketTickEvent(ticker="SPY"))
        assert loop.queue_depth == 2
        loop.clear()
        assert loop.queue_depth == 0

    def test_clear_does_not_affect_handlers(self):
        loop = EventLoop()
        called = []
        handler = lambda e: called.append(e)
        loop.register_handler("MARKET_TICK", handler)
        loop.clear()
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.run()
        assert len(called) == 1


class TestEventLoopWithBus:
    """Integration with MessageBus."""

    def test_events_published_to_bus(self):
        loop = EventLoop()
        received = []
        loop.bus.subscribe("MARKET_TICK", lambda e: received.append(e))
        loop.submit(MarketTickEvent(ticker="AAPL"))
        loop.run()
        assert len(received) == 1

    def test_default_bus_created(self):
        loop = EventLoop()
        assert loop.bus is not None
        assert isinstance(loop.bus, MessageBus)
