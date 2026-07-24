"""Shared fixtures for event_engine tests."""
from __future__ import annotations

import pytest


@pytest.fixture
def base_ts_ns() -> int:
    """An arbitrary starting timestamp in nanoseconds since epoch.

    The exact value does not matter — the tests are written so any
    monotonic baseline works. Using 1_700_000_000_000_000_000 ns
    (≈ 2023-11-14 UTC) keeps the values printable in failure dumps.
    """
    return 1_700_000_000_000_000_000


@pytest.fixture
def make_market(base_ts_ns):
    """Factory for valid MarketEvents with sensible defaults."""

    def _make(symbol: str = "AAPL", *, ts_offset: int = 0, **overrides):
        defaults = dict(
            timestamp_ns=base_ts_ns + ts_offset,
            symbol=symbol,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1_000.0,
            bid_ask_spread=0.01,
        )
        defaults.update(overrides)
        from event_engine.events import MarketEvent, BarType
        return MarketEvent(bar_type=BarType.BAR_1M, **defaults)

    return _make
