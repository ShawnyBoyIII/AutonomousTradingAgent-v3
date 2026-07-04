"""Tests for data.providers.base module (15 lines)."""

from __future__ import annotations

from typing import Any

from trading_bot.data.providers.base import MarketDataProvider


class _ValidProvider:
    def fetch_bars(
        self,
        symbol: str,
        period: str,
        interval: str,
        start: str | None = None,
        end: str | None = None,
    ) -> Any:
        return {"symbol": symbol}


class _MissingFetchBars:
    def something_else(self) -> None:
        pass


class TestMarketDataProviderProtocol:
    def test_importable(self) -> None:
        assert MarketDataProvider is not None

    def test_class_with_fetch_bars_is_instance(self) -> None:
        assert isinstance(_ValidProvider(), MarketDataProvider)

    def test_class_without_fetch_bars_is_not_instance(self) -> None:
        assert not isinstance(_MissingFetchBars(), MarketDataProvider)

    def test_plain_object_not_instance(self) -> None:
        assert not isinstance(object(), MarketDataProvider)

    def test_callable_attribute_counts(self) -> None:
        # runtime_checkable Protocol only checks attribute presence
        class _AttrOnly:
            fetch_bars = 42  # not callable, but attribute present

        assert isinstance(_AttrOnly(), MarketDataProvider)

    def test_fetch_bars_callable_invocation(self) -> None:
        provider = _ValidProvider()
        assert provider.fetch_bars("AAPL", period="5d", interval="1d") == {"symbol": "AAPL"}

    def test_fetch_bars_with_start_end(self) -> None:
        provider = _ValidProvider()
        result = provider.fetch_bars("SPY", period="1mo", interval="1d", start="2024-01-01", end="2024-02-01")
        assert result == {"symbol": "SPY"}