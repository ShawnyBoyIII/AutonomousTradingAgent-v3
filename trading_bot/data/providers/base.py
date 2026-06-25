from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MarketDataProvider(Protocol):
    def fetch_bars(
        self,
        symbol: str,
        period: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
    ) -> Any: ...
