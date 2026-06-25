from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.runtime.latency import (
    data_age_hours,
    frame_last_timestamp,
    is_stale,
)

ET = ZoneInfo("America/New_York")


def test_frame_last_timestamp_uses_timestamp_column() -> None:
    frame = pd.DataFrame(
        {"timestamp": pd.to_datetime(["2026-06-15", "2026-06-18"]), "close": [100.0, 105.0]}
    )

    value = frame_last_timestamp(frame)

    assert value == datetime(2026, 6, 18)


def test_frame_last_timestamp_falls_back_to_index() -> None:
    frame = pd.DataFrame(
        {"close": [100.0, 105.0]},
        index=pd.to_datetime(["2026-06-15", "2026-06-18"]),
    )

    value = frame_last_timestamp(frame)

    assert value == datetime(2026, 6, 18)


def test_frame_last_timestamp_returns_none_for_empty_frame() -> None:
    frame = pd.DataFrame({"timestamp": pd.to_datetime([]), "close": []})

    assert frame_last_timestamp(frame) is None


def test_frame_last_timestamp_returns_none_for_none_frame() -> None:
    assert frame_last_timestamp(None) is None


def test_data_age_hours_returns_delta_in_hours() -> None:
    then = datetime(2026, 6, 15, 12, 0, tzinfo=ET)
    now = datetime(2026, 6, 18, 12, 0, tzinfo=ET)

    assert data_age_hours(then, now) == 72.0


def test_data_age_hours_returns_zero_for_future_timestamp() -> None:
    future = datetime(2026, 6, 20, tzinfo=ET)
    now = datetime(2026, 6, 18, tzinfo=ET)

    assert data_age_hours(future, now) == 0.0


def test_data_age_hours_returns_none_for_missing_timestamp() -> None:
    now = datetime(2026, 6, 18, tzinfo=ET)
    assert data_age_hours(None, now) is None


def test_data_age_hours_handles_naive_vs_aware_mismatch() -> None:
    naive_then = datetime(2026, 6, 15, 12, 0)
    aware_now = datetime(2026, 6, 18, 12, 0, tzinfo=ET)

    age = data_age_hours(naive_then, aware_now)

    assert age == 72.0


def test_is_stale_marks_old_data_stale() -> None:
    then = datetime(2026, 6, 10, tzinfo=ET)
    now = datetime(2026, 6, 18, tzinfo=ET)

    assert is_stale(then, now, max_age_hours=72) is True


def test_is_stale_marks_fresh_data_fresh() -> None:
    then = datetime(2026, 6, 17, tzinfo=ET)
    now = datetime(2026, 6, 18, tzinfo=ET)

    assert is_stale(then, now, max_age_hours=72) is False


def test_is_stale_marks_missing_timestamp_as_stale() -> None:
    now = datetime(2026, 6, 18, tzinfo=ET)
    assert is_stale(None, now, max_age_hours=72) is True


def test_is_stale_respects_custom_threshold() -> None:
    then = datetime(2026, 6, 18, 8, 0, tzinfo=ET)
    now = datetime(2026, 6, 18, 12, 0, tzinfo=ET)

    assert is_stale(then, now, max_age_hours=1) is True
    assert is_stale(then, now, max_age_hours=8) is False
