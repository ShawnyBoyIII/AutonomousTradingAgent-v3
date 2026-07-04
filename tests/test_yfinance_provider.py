"""Tests for data.providers.yfinance_provider module (46 lines)."""

from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

from trading_bot.data.providers.yfinance_provider import YFinanceProvider


def _make_yfinance_module(
    *,
    history_frame: pd.DataFrame | None = None,
    screen_result=None,
) -> types.ModuleType:
    mod = types.ModuleType("yfinance")

    class _Ticker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **kwargs):  # noqa: ANN003
            return history_frame if history_frame is not None else pd.DataFrame()

    def _screen(source, count):  # noqa: ANN001
        return screen_result

    mod.Ticker = _Ticker
    mod.screen = _screen
    return mod


def _ohlcv_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "High": [11.0, 12.0],
            "Low": [9.5, 10.5],
            "Close": [10.5, 11.5],
            "Volume": [1000, 2000],
        },
        index=pd.DatetimeIndex(pd.to_datetime(["2024-01-01", "2024-01-02"])),
    )


class TestFetchBars:
    def test_period_path_normalizes_frame(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _make_yfinance_module(history_frame=_ohlcv_frame())
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        result = provider.fetch_bars("AAPL", period="5d", interval="1d")
        assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert len(result) == 2

    def test_start_end_path_uses_history_kwargs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        mod = types.ModuleType("yfinance")

        class _Ticker:
            def __init__(self, symbol):  # noqa: ANN001
                self.symbol = symbol

            def history(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return _ohlcv_frame()

        mod.Ticker = _Ticker
        mod.screen = lambda *a, **k: {}
        monkeypatch.setitem(sys.modules, "yfinance", mod)

        provider = YFinanceProvider()
        provider.fetch_bars("AAPL", period="ignored", interval="1d", start="2024-01-01", end="2024-01-10")
        assert captured["start"] == "2024-01-01"
        assert captured["end"] == "2024-01-10"
        assert captured["interval"] == "1d"
        assert captured["auto_adjust"] is False
        assert "period" not in captured

    def test_only_start_provided_uses_start_end_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        mod = types.ModuleType("yfinance")

        class _Ticker:
            def __init__(self, symbol):  # noqa: ANN001
                pass

            def history(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return _ohlcv_frame()

        mod.Ticker = _Ticker
        mod.screen = lambda *a, **k: {}
        monkeypatch.setitem(sys.modules, "yfinance", mod)

        provider = YFinanceProvider()
        provider.fetch_bars("AAPL", period="irrelevant", interval="1d", start="2024-01-01")
        assert "start" in captured
        assert "end" in captured
        assert "period" not in captured

    def test_empty_frame_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _make_yfinance_module(history_frame=pd.DataFrame())
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        with pytest.raises(ValueError, match="No market data returned for SPY"):
            provider.fetch_bars("SPY", period="5d", interval="1d")


class TestFetchSmallCapCandidates:
    def test_default_screeners_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default screeners list has two entries; each call returns the same quotes.
        results = iter(
            [
                {"quotes": [{"symbol": "ABC"}, {"symbol": "def"}]},
                {"quotes": [{"symbol": "ABC"}, {"symbol": "def"}]},
            ]
        )
        mod = types.ModuleType("yfinance")

        def _screen(source, count):  # noqa: ANN001
            return next(results)

        class _T:  # noqa: ANN
            def __init__(self, s):  # noqa: ANN001
                pass

            def history(self, **k):  # noqa: ANN003
                return pd.DataFrame()

        mod.Ticker = _T
        mod.screen = _screen
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        rows = provider.fetch_small_cap_candidates()
        # Two default screeners -> 2 quotes each = 4 rows
        assert len(rows) == 4
        symbols = [r["symbol"] for r in rows]
        assert symbols == ["ABC", "def", "ABC", "def"]
        assert rows[0]["source"] == "aggressive_small_caps"
        assert rows[-1]["source"] == "small_cap_gainers"

    def test_custom_screeners(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = {"quotes": [{"symbol": "XYZ"}]}
        mod = _make_yfinance_module(screen_result=result)
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        rows = provider.fetch_small_cap_candidates(screeners=["custom"])
        assert rows[0]["source"] == "custom"

    def test_skips_non_dict_quotes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = {"quotes": [{"symbol": "OK"}, "bad", None, 42]}
        mod = _make_yfinance_module(screen_result=result)
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        rows = provider.fetch_small_cap_candidates(screeners=["only"])
        assert len(rows) == 1
        assert rows[0]["symbol"] == "OK"

    def test_skips_empty_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = {"quotes": [{"symbol": ""}, {"symbol": "   "}, {"no_symbol": 1}]}
        mod = _make_yfinance_module(screen_result=result)
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        rows = provider.fetch_small_cap_candidates()
        assert rows == []

    def test_non_dict_result_treated_as_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mod = _make_yfinance_module(screen_result=None)
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        rows = provider.fetch_small_cap_candidates()
        assert rows == []

    def test_count_capped_at_250(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        mod = types.ModuleType("yfinance")

        def _screen(source, count):  # noqa: ANN001
            captured["count"] = count
            return {"quotes": []}

        class _T:  # noqa: ANN
            def __init__(self, s):  # noqa: ANN001
                pass

            def history(self, **k):  # noqa: ANN003
                return pd.DataFrame()

        mod.Ticker = _T
        mod.screen = _screen
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        provider.fetch_small_cap_candidates(limit=500)
        assert captured["count"] == 250

    def test_merges_multiple_screeners(self, monkeypatch: pytest.MonkeyPatch) -> None:
        results = iter(
            [
                {"quotes": [{"symbol": "A"}]},
                {"quotes": [{"symbol": "B"}, {"symbol": "C"}]},
            ]
        )
        mod = types.ModuleType("yfinance")

        def _screen(source, count):  # noqa: ANN001
            return next(results)

        class _T:  # noqa: ANN
            def __init__(self, s):  # noqa: ANN001
                pass

            def history(self, **k):  # noqa: ANN003
                return pd.DataFrame()

        mod.Ticker = _T
        mod.screen = _screen
        monkeypatch.setitem(sys.modules, "yfinance", mod)
        provider = YFinanceProvider()
        rows = provider.fetch_small_cap_candidates(screeners=["s1", "s2"])
        assert [r["symbol"] for r in rows] == ["A", "B", "C"]
        assert rows[0]["source"] == "s1"
        assert rows[1]["source"] == "s2"