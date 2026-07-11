from __future__ import annotations

import pandas as pd
import pytest
import requests

from trading_bot.config.settings import MarketDataSettings
from trading_bot.data import market_data
from trading_bot.data.providers.finnhub_provider import _redact_query_secrets as redact_finnhub
from trading_bot.data.providers.polygon_provider import _redact_query_secrets as redact_polygon


def test_provider_error_redaction_hides_query_secrets() -> None:
    message = requests.exceptions.ConnectionError(
        "https://api.example.test/path?apiKey=abc123&token=def456"
    )

    redacted = redact_polygon(message)
    redacted += " " + redact_finnhub(message)

    assert "abc123" not in redacted
    assert "def456" not in redacted
    assert "apiKey=<redacted>" in redacted
    assert "token=<redacted>" in redacted


def test_fallback_fetch_falls_through_on_alpaca_api_error(monkeypatch) -> None:
    """Regression: Alpaca raises APIError on SIP subscription failures.

    The fallback chain must skip Alpaca and use the next provider (yfinance)
    rather than propagating the exception out to callers.
    """
    from alpaca.common.exceptions import APIError

    fallback_frame = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
        "open": [100.0, 101.0, 102.0],
        "high": [105.0, 106.0, 107.0],
        "low": [99.0, 100.0, 101.0],
        "close": [101.0, 102.0, 103.0],
        "volume": [1000, 1100, 1200],
    })

    class FailingAlpaca:
        def fetch_bars(self, symbol, period, interval, start=None, end=None):
            raise APIError("subscription does not permit querying recent SIP data")

    class WorkingYFinance:
        def fetch_bars(self, symbol, period, interval, start=None, end=None):
            return fallback_frame

    providers = {"alpaca": FailingAlpaca(), "yfinance": WorkingYFinance()}
    monkeypatch.setattr(
        market_data, "_resolve_provider_by_name", lambda name: providers[name]
    )

    settings = MarketDataSettings(providers=["alpaca", "yfinance"])

    result = market_data._fallback_fetch(
        "AAPL", "5d", "5m", primary_settings=settings
    )

    assert isinstance(result, pd.DataFrame)
    assert len(result) == 3
    assert float(result["close"].iloc[0]) == 101.0
