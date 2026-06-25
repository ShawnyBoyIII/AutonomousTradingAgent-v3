from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def frame_last_timestamp(frame: Any) -> datetime | None:
    """Return the last bar timestamp from a market data frame.

    Accepts pandas DataFrames with a `timestamp` column or a DatetimeIndex.
    Returns None when the frame is empty or no timestamp can be resolved
    so callers can fall through to a conservative skip-on-missing rule.
    """
    if frame is None:
        return None
    if hasattr(frame, "empty") and frame.empty:
        return None

    if hasattr(frame, "columns") and "timestamp" in frame.columns:
        try:
            series = frame["timestamp"]
            if len(series) == 0:
                return None
            value = series.iloc[-1]
        except Exception:
            return None
        return _coerce_timestamp(value)

    if hasattr(frame, "index") and len(frame.index) > 0:
        try:
            value = frame.index[-1]
        except Exception:
            return None
        return _coerce_timestamp(value)

    return None


def data_age_hours(timestamp: datetime | None, now: datetime) -> float | None:
    """Return the age of `timestamp` in hours relative to `now`.

    Returns None when `timestamp` is None so callers can distinguish
    "no data" from "stale data". Both inputs must be tz-aware (or both
    naive) to avoid silenttz-mismatch arithmetic.
    """
    if timestamp is None:
        return None

    normalized_now = now
    if timestamp.tzinfo is None and now.tzinfo is not None:
        normalized_now = now.replace(tzinfo=None)
    elif timestamp.tzinfo is not None and now.tzinfo is None:
        normalized_now = now.replace(tzinfo=timestamp.tzinfo)

    delta = normalized_now - timestamp
    if delta < timedelta(0):
        return 0.0
    return delta.total_seconds() / 3600.0


def data_age_minutes(timestamp: datetime | None, now: datetime) -> float | None:
    """Return the age of `timestamp` in minutes relative to `now`."""
    if timestamp is None:
        return None

    normalized_now = now
    if timestamp.tzinfo is None and now.tzinfo is not None:
        normalized_now = now.replace(tzinfo=None)
    elif timestamp.tzinfo is not None and now.tzinfo is None:
        normalized_now = now.replace(tzinfo=timestamp.tzinfo)

    delta = normalized_now - timestamp
    if delta < timedelta(0):
        return 0.0
    return delta.total_seconds() / 60.0


def is_stale(
    timestamp: datetime | None,
    now: datetime,
    max_age_hours: int | None = None,
    max_age_minutes: int | None = None,
) -> bool:
    """Decide whether a frame's last bar is too old to act on.

    Provide either `max_age_hours` or `max_age_minutes` (not both).
    For intraday data, use minutes. For daily data, use hours.
    """
    if timestamp is None:
        return True
    
    if max_age_minutes is not None:
        age = data_age_minutes(timestamp, now)
        if age is None:
            return True
        return age > float(max_age_minutes)
    
    age = data_age_hours(timestamp, now)
    if age is None:
        return True
    return age > float(max_age_hours)


def _coerce_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        import pandas as pd

        converted = pd.Timestamp(value)
        if converted is None or str(converted) in ("NaT", "nan"):
            return None
        return converted.to_pydatetime()
    except Exception:
        return None
