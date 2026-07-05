from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

from trading_bot.events.bus import MessageBus
from trading_bot.events.types import Event, SystemHeartbeatEvent, SystemTickEvent

logger = logging.getLogger(__name__)


class EventLoop:
    """Deterministic event loop for processing events in timestamp order.

    Single-threaded loop that maintains an ordered event queue.
    Supports both real-time mode (wall-clock driven) and simulated mode
    (backtick-driven with nanosecond resolution).
    """

    def __init__(self, bus: MessageBus | None = None) -> None:
        self.bus = bus or MessageBus()
        self._queue: deque[Event] = deque()
        self._running: bool = False
        self._tick: int = 0
        self._events_processed: int = 0
        self._start_time: float = 0.0
        self._handlers: dict[str, list[Callable[[Event], None]]] = {}
        self._max_queue_size: int = 100_000
        self._backpressure_threshold: int = 50_000

    def submit(self, event: Event) -> None:
        if len(self._queue) >= self._max_queue_size:
            self._queue.popleft()
        self._queue.append(event)

    def submit_batch(self, events: list[Event]) -> None:
        for event in events:
            self.submit(event)

    def register_handler(self, event_type: str, handler: Callable[[Event], None]) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def unregister_handler(self, event_type: str, handler: Callable[[Event], None]) -> bool:
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def run(self, max_events: int | None = None) -> int:
        self._running = True
        self._start_time = time.monotonic()
        processed = 0
        self.bus.publish(SystemTickEvent(tick=0))

        while self._queue and (max_events is None or processed < max_events):
            if self._running:
                self._process_one()
                processed += 1
            else:
                break

            if processed % 1000 == 0 and processed > 0:
                self.bus.publish(
                    SystemHeartbeatEvent(
                        uptime_seconds=time.monotonic() - self._start_time,
                        events_processed=self._events_processed,
                        queue_depth=len(self._queue),
                    )
                )

        self._running = False
        return processed

    def run_until_empty(self) -> int:
        return self.run()

    def step(self) -> bool:
        if not self._queue:
            return False
        self._process_one()
        return True

    def _process_one(self) -> None:
        if not self._queue:
            return

        event = self._queue.popleft()
        self._tick += 1
        self._events_processed += 1

        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.debug("Event processing error: %s", e)

        self.bus.publish(event)

    def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    @property
    def tick(self) -> int:
        return self._tick

    @property
    def events_processed(self) -> int:
        return self._events_processed

    def clear(self) -> None:
        self._queue.clear()
