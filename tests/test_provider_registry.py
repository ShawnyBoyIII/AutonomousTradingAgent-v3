from __future__ import annotations

import pandas as pd
import pytest

from trading_bot.cli.app import _format_doctor
from trading_bot.config.settings import MarketDataSettings, Settings
from trading_bot.data import market_data
from trading_bot.data.providers.registry import (
    get_provider_capabilities,
    order_provider_names,
    provider_readiness,
)


def test_registry_exposes_normalized_provider_capabilities() -> None:
    alpaca = get_provider_capabilities("alpaca")
    yfinance = get_provider_capabilities("yfinance")

    assert alpaca.asset_classes == frozenset({"equity"})
    assert alpaca.supports_interval("5m")
    assert alpaca.required_environment == (
        "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY",
    )
    assert not alpaca.supports_screening
    assert yfinance.supports_interval("90m")
    assert yfinance.supports_screening


def test_registry_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown market data provider"):
        get_provider_capabilities("unknown")


def test_provider_readiness_is_network_free_and_never_returns_secrets() -> None:
    missing = provider_readiness("finnhub", environ={})
    ready = provider_readiness("finnhub", environ={"FINNHUB_API_KEY": "secret-value"})

    assert not missing.ready
    assert missing.reason == "missing FINNHUB_API_KEY"
    assert ready.ready
    assert ready.reason == "ok"
    assert "secret-value" not in repr(ready)


def test_provider_order_preserves_daily_config_and_prioritizes_intraday() -> None:
    names = ["yfinance", "alpaca", "polygon"]

    assert order_provider_names(names, "1d") == names
    assert order_provider_names(names, "5m") == ["polygon", "alpaca", "yfinance"]


def test_fallback_skips_provider_that_cannot_supply_interval(monkeypatch) -> None:
    attempted: list[str] = []
    expected = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="90min"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000, 1100],
        }
    )

    class Provider:
        def fetch_bars(self, symbol, period, interval, start=None, end=None):
            return expected

    def resolve(name: str):
        attempted.append(name)
        return Provider()

    monkeypatch.setattr(market_data, "_resolve_provider_by_name", resolve)
    settings = MarketDataSettings(providers=["polygon", "yfinance"])

    result = market_data._fallback_fetch(
        "AAPL", "5d", "90m", primary_settings=settings
    )

    assert result is expected
    assert attempted == ["yfinance"]


def test_doctor_reports_finnhub_readiness_from_registry(monkeypatch) -> None:
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    settings = Settings(market_data=MarketDataSettings(providers=["finnhub"]))

    output = _format_doctor(settings)

    assert "provider_auth=finnhub:missing FINNHUB_API_KEY" in output
