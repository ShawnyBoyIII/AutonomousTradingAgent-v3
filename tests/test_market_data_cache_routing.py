from __future__ import annotations

import pandas as pd

from trading_bot.config.settings import MarketDataSettings
from trading_bot.data import market_data


def test_fetch_bars_uses_configured_cache_path(tmp_path, monkeypatch) -> None:
    cache_path = tmp_path / "market_data_cache.db"
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="D"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1_000, 1_100],
        }
    )
    monkeypatch.setattr(market_data, "_fallback_fetch", lambda *args, **kwargs: frame)

    market_data.fetch_bars(
        "TEST",
        "5d",
        "1d",
        settings=MarketDataSettings(cache_db_path=str(cache_path)),
    )

    assert cache_path.exists()
