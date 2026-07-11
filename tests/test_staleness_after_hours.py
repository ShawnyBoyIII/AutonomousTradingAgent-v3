"""Tests for the after-hours staleness allowance in manage-positions.

2026-07-09 incident: the prior version of
``_market_data_is_stale_for_manage`` only allowed 12-24h-old bars
after-hours, leaving a 4-12h dead-zone where same-day EOD bars were
rejected.  At 21:19 ET, the 15:55 ET EOD bar (5h24m old) was rejected
and 8 positions were SKIPped.

This module verifies the new behavior: any bar from the last 24h is
acceptable when the market is closed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trading_bot.cli.app import _market_data_is_stale_for_manage


_ET = ZoneInfo("America/New_York")


def test_same_day_eod_bar_is_fresh_after_hours() -> None:
    """A 5h-old EOD bar (typical after-hours check) is now accepted."""
    after_market = datetime(2026, 6, 18, 21, 19, tzinfo=_ET)  # Thu 21:19 ET
    eod_bar = datetime(2026, 6, 18, 15, 55, tzinfo=_ET)  # same day 15:55 ET
    # age = 5h24m = 324 minutes
    assert (after_market - eod_bar).total_seconds() / 60 == 324
    assert _market_data_is_stale_for_manage(eod_bar, after_market, 120) is False


def test_yesterday_close_is_fresh_after_hours() -> None:
    """Yesterday's close bar (18h old) is fresh — original behavior preserved."""
    after_close = datetime(2026, 6, 18, 19, 0, tzinfo=_ET)
    prior_close_bar = after_close - timedelta(hours=18)
    assert _market_data_is_stale_for_manage(prior_close_bar, after_close, 120) is False


def test_fresh_bar_is_fresh_after_hours() -> None:
    """A 1-minute-old bar is fresh (same-day)."""
    after_close = datetime(2026, 6, 18, 19, 0, tzinfo=_ET)
    one_minute_ago = after_close - timedelta(minutes=1)
    assert _market_data_is_stale_for_manage(one_minute_ago, after_close, 120) is False


def test_25h_old_bar_is_stale_after_hours() -> None:
    """A 25h-old bar (weekend case) is still rejected."""
    weekend = datetime(2026, 6, 20, 12, 0, tzinfo=_ET)  # Saturday
    weekend_bar = weekend - timedelta(hours=40)
    assert _market_data_is_stale_for_manage(weekend_bar, weekend, 120) is True


def test_just_under_24h_bar_is_fresh() -> None:
    """A 23h59m bar is still within the 24h allowance."""
    after_close = datetime(2026, 6, 18, 19, 0, tzinfo=_ET)
    bar = after_close - timedelta(hours=23, minutes=59)
    assert _market_data_is_stale_for_manage(bar, after_close, 120) is False


def test_just_over_24h_bar_is_stale() -> None:
    """A 24h01m bar is over the 24h allowance."""
    after_close = datetime(2026, 6, 18, 19, 0, tzinfo=_ET)
    bar = after_close - timedelta(hours=24, minutes=1)
    assert _market_data_is_stale_for_manage(bar, after_close, 120) is True


def test_during_market_hours_stale_bar_is_rejected() -> None:
    """During market hours, a 130m-old bar (over 120m threshold) is rejected."""
    market_open = datetime(2026, 6, 18, 12, 0, tzinfo=_ET)
    stale_in_hours = market_open - timedelta(minutes=130)
    assert _market_data_is_stale_for_manage(stale_in_hours, market_open, 120) is True


def test_during_market_hours_fresh_bar_is_accepted() -> None:
    """During market hours, a 5m-old bar is fresh."""
    market_open = datetime(2026, 6, 18, 12, 0, tzinfo=_ET)
    fresh_in_hours = market_open - timedelta(minutes=5)
    assert _market_data_is_stale_for_manage(fresh_in_hours, market_open, 120) is False


def test_none_timestamp_is_stale() -> None:
    """A None last_timestamp always reports stale."""
    now = datetime(2026, 6, 18, 19, 0, tzinfo=_ET)
    assert _market_data_is_stale_for_manage(None, now, 120) is True
