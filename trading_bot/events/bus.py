from __future__ import annotations

import fnmatch
import logging
from collections import defaultdict
from typing import Any, Callable

from trading_bot.events.types import Event

logger = logging.getLogger(__name__)


class MessageBus:
    """Pub/sub message bus for decoupled event-driven component communication.

    Supports topic-based routing with wildcard patterns (e.g. "MARKET.*", "ORDER.*").
    Handlers are called synchronously in subscription order per topic.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Event], None]]] = defaultdict(list)
        self._event_log: list[Event] = []
        self._max_log_size: int = 100_000

    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None:
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[Event], None]) -> bool:
        handlers = self._subscribers.get(topic, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def publish(self, event: Event) -> None:
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size // 2:]

        for topic, handlers in self._subscribers.items():
            if self._topic_matches(topic, event):
                for handler in handlers:
                    try:
                        handler(event)
                    except Exception as e:
                        logger.debug("Handler error in publish: %s", e)

    def publish_to(self, topic: str, event: Event) -> None:
        self._event_log.append(event)
        if len(self._event_log) > self._max_log_size:
            self._event_log = self._event_log[-self._max_log_size // 2:]

        handlers = self._subscribers.get(topic, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                logger.debug("Handler error in publish_to: %s", e)

    def _topic_matches(self, topic: str, event: Event) -> bool:
        if topic == "*":
            return True
        if topic == event.event_type:
            return True
        if fnmatch.fnmatch(event.event_type, topic):
            return True
        return False

    def get_recent(self, n: int = 100) -> list[Event]:
        return self._event_log[-n:]

    def clear_log(self) -> None:
        self._event_log.clear()

    @property
    def log_size(self) -> int:
        return len(self._event_log)
