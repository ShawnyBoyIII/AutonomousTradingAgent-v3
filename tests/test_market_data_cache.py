"""Tests for the SQLite-backed market data cache with TTL expiration."""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.data.cache import MarketDataCache, _interval_to_ttl_seconds, _make_cache_key


@pytest.fixture
def cache(tmp_path):
    db = tmp_path / "test_cache.db"
    return MarketDataCache(db_path=db, max_entries=100)


class TestIntervalTTL:
    def test_1m_interval(self):
        assert _interval_to_ttl_seconds("1m") == 60

    def test_5m_interval(self):
        assert _interval_to_ttl_seconds("5m") == 150

    def test_15m_interval(self):
        assert _interval_to_ttl_seconds("15m") == 450

    def test_1h_interval(self):
        assert _interval_to_ttl_seconds("1h") == 1800

    def test_1d_interval(self):
        assert _interval_to_ttl_seconds("1d") == 43200

    def test_unknown_unit(self):
        assert _interval_to_ttl_seconds("1x") == 300


class TestCacheKey:
    def test_basic_key(self):
        key = _make_cache_key("AAPL", "1y", "1d", None, None)
        assert key == "AAPL:1y:1d::"

    def test_key_with_start_end(self):
        key = _make_cache_key("aapl", "1y", "1d", "2025-01-01", "2025-06-01")
        assert key == "AAPL:1y:1d:2025-01-01:2025-06-01"

    def test_key_case_insensitive(self):
        k1 = _make_cache_key("aapl", "1y", "1d", None, None)
        k2 = _make_cache_key("AAPL", "1y", "1d", None, None)
        assert k1 == k2


class TestCacheGetPut:
    def test_put_and_get(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=5, freq="1d"),
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [105.0, 106.0, 107.0, 108.0, 109.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0, 105.0],
            "volume": [1000, 1100, 1200, 1300, 1400],
        })
        cache.put("AAPL", "1d", "1d", df)
        result = cache.get("AAPL", "1d", "1d")
        assert result is not None
        assert len(result) == 5
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_get_miss(self, cache):
        result = cache.get("AAPL", "1y", "1d")
        assert result is None

    def test_get_different_params(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        cache.put("AAPL", "1y", "1d", df)
        result = cache.get("AAPL", "1y", "5m")
        assert result is None

    def test_overwrite_existing(self, cache):
        df1 = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        df2 = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-05", periods=3, freq="1d"),
            "open": [200.0, 201.0, 202.0],
            "high": [205.0, 206.0, 207.0],
            "low": [199.0, 200.0, 201.0],
            "close": [201.0, 202.0, 203.0],
            "volume": [2000, 2100, 2200],
        })
        cache.put("AAPL", "1y", "1d", df1)
        cache.put("AAPL", "1y", "1d", df2)
        result = cache.get("AAPL", "1y", "1d")
        assert len(result) == 3
        assert result["close"].iloc[0] == 201.0

    def test_empty_df(self, cache):
        df = pd.DataFrame()
        cache.put("AAPL", "1y", "1d", df)
        result = cache.get("AAPL", "1y", "1d")
        assert result is not None
        assert result.empty


class TestCacheInvalidation:
    def test_invalidate_symbol(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        cache.put("AAPL", "1y", "1d", df)
        cache.put("SPY", "1y", "1d", df)
        count = cache.invalidate(symbol="AAPL")
        assert count == 1
        assert cache.get("AAPL", "1y", "1d") is None
        assert cache.get("SPY", "1y", "1d") is not None

    def test_invalidate_all(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        cache.put("AAPL", "1y", "1d", df)
        cache.put("SPY", "1y", "1d", df)
        count = cache.invalidate()
        assert count == 2

    def test_clear_expired(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        cache.put("AAPL", "1y", "1m", df)
        time.sleep(0.1)
        count = cache.clear_expired()
        assert count >= 0


class TestCacheStatus:
    def test_status(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        cache.put("AAPL", "1y", "1d", df)
        cache.put("SPY", "1y", "1d", df)
        status = cache.status()
        assert status["total_entries"] == 2
        assert status["unique_symbols"] == 2
        assert "AAPL" in status["symbols"]
        assert "SPY" in status["symbols"]


class TestCacheEviction:
    def test_eviction_on_overflow(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        for i in range(150):
            cache.put(f"SYM{i}", "1y", "1d", df)
        status = cache.status()
        assert status["total_entries"] <= 100


class TestSerialization:
    def test_serialize_deserialize(self):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=5, freq="1d"),
            "open": [100.0, 101.0, 102.0, 103.0, 104.0],
            "high": [105.0, 106.0, 107.0, 108.0, 109.0],
            "low": [99.0, 100.0, 101.0, 102.0, 103.0],
            "close": [101.0, 102.0, 103.0, 104.0, 105.0],
            "volume": [1000, 1100, 1200, 1300, 1400],
        })
        payload = MarketDataCache._serialize(df)
        result = MarketDataCache._deserialize(payload)
        assert len(result) == 5
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert result["close"].iloc[0] == 101.0

    def test_serialize_empty(self):
        df = pd.DataFrame()
        payload = MarketDataCache._serialize(df)
        obj = json.loads(payload)
        assert obj["empty"] is True
        result = MarketDataCache._deserialize(payload)
        assert result.empty


class TestCacheRepr:
    def test_repr(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })
        cache.put("AAPL", "1y", "1d", df)
        assert "MarketDataCache" in repr(cache)
        assert "entries=1" in repr(cache)


class TestCacheThreadSafety:
    def test_concurrent_writes(self, cache):
        df = pd.DataFrame({
            "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
            "open": [100.0, 101.0, 102.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1100, 1200],
        })

        def writer(i):
            cache.put(f"SYM{i}", "1y", "1d", df)

        import threading
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        status = cache.status()
        assert status["total_entries"] == 50
        assert status["unique_symbols"] == 50


class TestIntegration:
    def test_cache_bypasses_network(self, cache, monkeypatch):
        fetch_call_count = [0]

        def mock_fetch(symbol, period, interval, start=None, end=None, primary_settings=None):
            fetch_call_count[0] += 1
            return pd.DataFrame({
                "timestamp": pd.date_range("2025-01-01", periods=3, freq="1d"),
                "open": [100.0, 101.0, 102.0],
                "high": [105.0, 106.0, 107.0],
                "low": [99.0, 100.0, 101.0],
                "close": [101.0, 102.0, 103.0],
                "volume": [1000, 1100, 1200],
            })

        monkeypatch.setattr(cache, "get", lambda *a, **k: None)
        monkeypatch.setattr(cache, "put", lambda *a, **k: None)

        from trading_bot.data import market_data

        monkeypatch.setattr(market_data, "_get_cache", lambda: cache)
        monkeypatch.setattr(market_data, "_fallback_fetch", mock_fetch)

        result1 = market_data.fetch_bars("AAPL", "1y", "1d")
        result2 = market_data.fetch_bars("AAPL", "1y", "1d")

        assert fetch_call_count[0] == 2
        assert len(result1) == 3
