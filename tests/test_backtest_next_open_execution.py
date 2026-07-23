from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from trading_bot.backtest import runner
from trading_bot.config.settings import Settings
from trading_bot.models.signal import TradeSignal


def _signal() -> TradeSignal:
    return TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=101.0,
        stop_loss=99.0,
        profit_target=110.0,
        risk_reward_ratio=4.5,
        confidence=0.9,
        reasons=["next-open test"],
        strategy_tag="v2.5",
        timestamp=datetime(2026, 7, 10, 14, 20, tzinfo=timezone.utc),
    )


def _intraday_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-07-10 14:00:00+00:00", periods=7, freq="5min"),
            "open": [100.0, 100.0, 100.0, 100.0, 101.0, 102.0, 109.0],
            "high": [101.0, 101.0, 101.0, 101.0, 102.0, 103.0, 111.0],
            "low": [99.0, 99.0, 99.0, 99.0, 100.0, 101.0, 108.0],
            "close": [100.0, 100.0, 100.0, 100.0, 101.0, 102.0, 110.0],
            "volume": [1000] * 7,
            "volume_avg_5": [1000.0] * 7,
        }
    )


def test_signal_is_repriced_to_next_bar_open() -> None:
    bar = pd.Series(
        {
            "open": 102.0,
            "timestamp": pd.Timestamp("2026-07-10 14:25:00+00:00"),
        }
    )

    repriced = runner._signal_at_next_open(_signal(), bar)

    assert repriced is not None
    assert repriced.entry_price == 102.0
    assert repriced.risk_reward_ratio == pytest.approx(8.0 / 3.0)
    assert repriced.timestamp == datetime(2026, 7, 10, 14, 25, tzinfo=timezone.utc)


@pytest.mark.parametrize("next_open", [98.0, 99.0, 110.0, 111.0])
def test_signal_is_cancelled_when_next_open_invalidates_setup(next_open: float) -> None:
    bar = pd.Series(
        {
            "open": next_open,
            "timestamp": pd.Timestamp("2026-07-10 14:25:00+00:00"),
        }
    )

    assert runner._signal_at_next_open(_signal(), bar) is None


def test_intraday_backtest_fills_approved_signal_at_next_open(monkeypatch) -> None:
    generated = 0

    def fake_generate(symbol, daily_frame, intraday_window):
        nonlocal generated
        generated += 1
        return _signal() if generated == 1 else None

    monkeypatch.setattr(runner, "generate_signal", fake_generate)
    settings = Settings()
    settings.paper.fee_per_order = 0.0
    settings.paper.slippage_bps = 0

    result = runner._run_symbol_backtest(
        "AAPL",
        pd.DataFrame({"close": [100.0]}),
        _intraday_frame(),
        settings,
    )

    assert result["trades"] == 1
    assert result["net_pnl"] == 152.0
