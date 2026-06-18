from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trading_bot.config.settings import SessionSettings


def now_in_zone(timezone: str) -> datetime:
    """Return tz-aware current time in `timezone`.

    Tests monkeypatch this single function to control the wall clock for
    the EOD check. Naive datetimes are never used inside the manager.
    """
    return datetime.now(ZoneInfo(timezone))


def should_eod_exit(now: datetime, settings: SessionSettings) -> bool:
    """Decide whether the manager should flatten open positions.

    Returns True only when:
      - EOD exits are enabled in config
      - `now` is a weekday (Mon-Fri)
      - local time-of-day is at or after `close_hour:close_minute - minutes_before_close`

    The `now` argument must be tz-aware; the timezone attached to it is
    the one used for the comparison (typically America/New_York).
    """
    if not settings.eod_enabled:
        return False

    if now.weekday() >= 5:
        return False

    close_time_of_day = timedelta(
        hours=settings.close_hour, minutes=settings.close_minute
    )
    threshold = close_time_of_day - timedelta(minutes=settings.eod_minutes_before_close)
    if threshold < timedelta(0):
        threshold = timedelta(0)

    current_time_of_day = timedelta(hours=now.hour, minutes=now.minute)
    return current_time_of_day >= threshold
