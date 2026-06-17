import pandas as pd
import pytest

from trading_bot.config.settings import Settings
from trading_bot.backtest.metrics import compute_win_rate
from trading_bot.backtest.runner import _filter_frame_by_date, _run_symbol_backtest, iterate_bars


def test_iterate_bars_yields_chronological_slices() -> None:
    frame = pd.DataFrame({"close": [1, 2, 3, 4]})
    slices = list(iterate_bars(frame, warmup=2))

    assert len(slices) == 2
    assert list(slices[0]["close"]) == [1, 2]
    assert list(slices[1]["close"]) == [1, 2, 3]


def test_iterate_bars_rejects_non_positive_warmup() -> None:
    frame = pd.DataFrame({"close": [1, 2, 3, 4]})

    with pytest.raises(ValueError, match="warmup must be positive"):
        list(iterate_bars(frame, warmup=0))


def test_compute_win_rate_returns_fraction_and_zero_safe_default() -> None:
    assert compute_win_rate(3, 1) == 0.75
    assert compute_win_rate(0, 0) == 0.0


def test_filter_frame_by_date_handles_tz_aware_timestamps() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-01 10:00:00-04:00",
                    "2026-06-10 10:00:00-04:00",
                    "2026-06-20 10:00:00-04:00",
                ]
            ),
            "close": [1.0, 2.0, 3.0],
        }
    )

    filtered = _filter_frame_by_date(frame, start="2026-06-05", end="2026-06-17")

    assert list(filtered["close"]) == [2.0]


def test_run_symbol_backtest_counts_stop_hit_as_loss_even_if_final_close_recovers() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0 + index for index in range(60)],
            "ema_20": [90.0 + index for index in range(60)],
            "sma_50": [80.0 + index for index in range(60)],
        }
    )
    intraday = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                    "2026-06-13 10:25:00",
                    "2026-06-13 10:30:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0, 102.5],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 101.1, 103.5],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 99.7, 102.0],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 100.8, 103.0],
            "volume": [1000, 1100, 950, 1050, 2500, 1500, 1800],
        }
    )
    intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()

    result = _run_symbol_backtest("AAPL", daily, intraday, Settings())

    assert result == {
        "trades": 1,
        "wins": 0,
        "losses": 1,
        "net_pnl": -101.6,
    }


def test_run_symbol_backtest_replays_multiple_trade_cycles() -> None:
    daily = pd.DataFrame(
        {
            "close": [100.0 + index for index in range(60)],
            "ema_20": [90.0 + index for index in range(60)],
            "sma_50": [80.0 + index for index in range(60)],
        }
    )
    intraday = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-06-13 10:00:00",
                    "2026-06-13 10:05:00",
                    "2026-06-13 10:10:00",
                    "2026-06-13 10:15:00",
                    "2026-06-13 10:20:00",
                    "2026-06-13 10:25:00",
                    "2026-06-13 10:30:00",
                    "2026-06-13 10:35:00",
                    "2026-06-13 10:40:00",
                    "2026-06-13 10:45:00",
                    "2026-06-13 10:50:00",
                    "2026-06-13 10:55:00",
                ]
            ),
            "open": [99.9, 100.1, 100.0, 100.2, 100.5, 101.0, 101.5, 101.6, 101.8, 102.0, 102.2, 102.8],
            "high": [100.1, 100.3, 100.2, 100.4, 101.1, 103.2, 101.7, 101.9, 102.1, 102.3, 103.4, 103.6],
            "low": [99.8, 100.0, 99.9, 100.1, 100.4, 100.9, 101.4, 101.5, 101.7, 101.9, 102.1, 102.6],
            "close": [100.0, 100.2, 100.1, 100.3, 101.0, 103.0, 101.6, 101.8, 102.0, 102.2, 103.2, 103.4],
            "volume": [1000, 1100, 950, 1050, 2500, 1500, 900, 950, 1000, 1100, 2600, 1700],
        }
    )
    intraday["volume_avg_5"] = intraday["volume"].rolling(5).mean()

    result = _run_symbol_backtest("AAPL", daily, intraday, Settings())

    assert result == {
        "trades": 2,
        "wins": 2,
        "losses": 0,
        "net_pnl": 206.2,
    }
