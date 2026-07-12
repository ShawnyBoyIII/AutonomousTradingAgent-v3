from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from trading_bot.backtest import runner
from trading_bot.config.settings import Settings
from trading_bot.models.signal import TradeSignal


def _intraday_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-07-10 14:00:00+00:00", periods=7, freq="5min"),
            "open": [100.0, 100.0, 100.0, 100.0, 101.0, 101.0, 104.0],
            "high": [101.0, 101.0, 101.0, 101.0, 102.0, 102.0, 106.0],
            "low": [99.0, 99.0, 99.0, 99.0, 100.0, 100.0, 103.0],
            "close": [100.0, 100.0, 100.0, 100.0, 101.0, 101.0, 105.0],
            "volume": [1000] * 7,
            "volume_avg_5": [1000.0] * 7,
        }
    )


def _signal() -> TradeSignal:
    return TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=101.0,
        stop_loss=99.0,
        profit_target=105.0,
        risk_reward_ratio=2.0,
        confidence=0.9,
        reasons=["causal test"],
        strategy_tag="v2.5",
        timestamp=datetime(2026, 7, 10, 14, 20, tzinfo=timezone.utc),
    )


def test_intraday_backtest_does_not_call_future_scanning_exit_resolver(monkeypatch) -> None:
    generated = 0

    def fake_generate(symbol, daily_frame, intraday_window):
        nonlocal generated
        generated += 1
        return _signal() if generated == 1 else None

    monkeypatch.setattr(runner, "generate_signal", fake_generate)
    assert not hasattr(runner, "_resolve_exit")

    result = runner._run_symbol_backtest(
        "AAPL",
        pd.DataFrame({"close": [100.0]}),
        _intraday_frame(),
        Settings(),
    )

    assert result["trades"] == 1
    assert result["wins"] == 1


def test_intraday_backtest_hides_future_daily_rows_from_signal_generation(monkeypatch) -> None:
    daily = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-07-09T20:00:00Z", "2026-07-10T20:00:00Z", "2026-07-13T20:00:00Z"]
            ),
            "close": [99.0, 100.0, 1000.0],
        }
    )

    def fake_generate(symbol, daily_window, intraday_window):
        current = pd.Timestamp(intraday_window["timestamp"].iloc[-1])
        assert pd.Timestamp(daily_window["timestamp"].max()).date() < current.date()
        return None

    monkeypatch.setattr(runner, "generate_signal", fake_generate)

    runner._run_symbol_backtest("AAPL", daily, _intraday_frame(), Settings())


def test_same_bar_stop_and_target_collision_uses_stop() -> None:
    signal = _signal()
    bar = pd.Series({"low": 98.0, "high": 106.0})

    assert runner._bar_exit(signal, bar) == (99.0, "stop")
