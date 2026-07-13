"""Regression: normalize_ohlcv_frame must keep bar timestamps as the DataFrame
index so downstream code (e.g. signal_quality.evaluate_entry_timing) reads the
real bar time via .index[-1] instead of epoch-zero garbage from reset_index.
"""
from __future__ import annotations

from datetime import time

import pandas as pd
import pytest
from zoneinfo import ZoneInfo

from trading_bot.data.market_data import normalize_ohlcv_frame

ET = ZoneInfo("America/New_York")


def _raw_yfinance_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [10.0, 11.0, 12.0],
            "High": [10.5, 11.5, 12.5],
            "Low": [9.8, 10.8, 11.8],
            "Close": [10.4, 11.2, 12.1],
            "Volume": [1000, 1500, 2000],
        },
        index=pd.DatetimeIndex(
            pd.to_datetime(
                ["2026-07-07 14:30:00", "2026-07-07 14:35:00", "2026-07-07 14:45:00"]
            ).tz_localize(ET)
        ),
    )


def test_normalize_preserves_timestamp_column() -> None:
    result = normalize_ohlcv_frame(_raw_yfinance_frame())
    assert list(result.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_normalize_keeps_bar_timestamps_in_index() -> None:
    """The bug: reset_index moved the DatetimeIndex into a regular column
    and left the frame with a RangeIndex. Downstream code that reads
    bars.index[-1] (signal_quality) then gets integer row numbers that
    decode to 1970-01-01 and trigger _is_avoid_time on epoch-zero."""
    result = normalize_ohlcv_frame(_raw_yfinance_frame())

    last = result.index[-1]
    assert isinstance(last, pd.Timestamp), (
        f"BUG: index[-1] is {last!r} (type {type(last).__name__}), "
        f"expected a pd.Timestamp from the source DatetimeIndex."
    )

    local = last.astimezone(ET).time() if last.tzinfo is not None else last.replace(tzinfo=ET).time()
    assert local.hour == 14, (
        f"BUG: index[-1] in ET is {local}; expected 14:XX from the raw frame. "
        f"This causes evaluate_entry_timing._is_avoid_time to fire on the wrong "
        f"hour and reject every signal."
    )


def test_normalize_first_bar_in_index_matches_first_column() -> None:
    result = normalize_ohlcv_frame(_raw_yfinance_frame())
    assert result.index[0] == result["timestamp"].iloc[0]
