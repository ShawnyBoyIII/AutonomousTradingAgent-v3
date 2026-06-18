from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trading_bot.config.settings import SessionSettings
from trading_bot.runtime.session import now_in_zone, should_eod_exit


ET = ZoneInfo("America/New_York")


def test_now_in_zone_is_tz_aware() -> None:
    now = now_in_zone("America/New_York")
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


def test_should_eod_exit_returns_true_at_threshold() -> None:
    settings = SessionSettings()
    now = datetime(2026, 6, 18, 15, 55, tzinfo=ET)  # Thursday 15:55 ET
    assert should_eod_exit(now, settings) is True


def test_should_eod_exit_returns_true_after_threshold() -> None:
    settings = SessionSettings()
    now = datetime(2026, 6, 18, 15, 59, tzinfo=ET)
    assert should_eod_exit(now, settings) is True


def test_should_eod_exit_returns_true_during_final_minute() -> None:
    settings = SessionSettings()
    now = datetime(2026, 6, 18, 16, 0, tzinfo=ET)
    assert should_eod_exit(now, settings) is True


def test_should_eod_exit_returns_false_before_threshold() -> None:
    settings = SessionSettings()
    now = datetime(2026, 6, 18, 15, 54, tzinfo=ET)
    assert should_eod_exit(now, settings) is False


def test_should_eod_exit_returns_false_during_trading_hours() -> None:
    settings = SessionSettings()
    now = datetime(2026, 6, 18, 10, 30, tzinfo=ET)
    assert should_eod_exit(now, settings) is False


def test_should_eod_exit_returns_false_on_saturday() -> None:
    settings = SessionSettings()
    now = datetime(2026, 6, 20, 15, 55, tzinfo=ET)  # Saturday
    assert should_eod_exit(now, settings) is False


def test_should_eod_exit_returns_false_on_sunday() -> None:
    settings = SessionSettings()
    now = datetime(2026, 6, 21, 15, 55, tzinfo=ET)  # Sunday
    assert should_eod_exit(now, settings) is False


def test_should_eod_exit_respects_custom_minutes_before_close() -> None:
    settings = SessionSettings(close_hour=16, close_minute=0, eod_minutes_before_close=30)
    assert should_eod_exit(datetime(2026, 6, 18, 15, 30, tzinfo=ET), settings) is True
    assert should_eod_exit(datetime(2026, 6, 18, 15, 29, tzinfo=ET), settings) is False


def test_should_eod_exit_can_be_disabled() -> None:
    settings = SessionSettings(eod_enabled=False)
    now = datetime(2026, 6, 18, 15, 55, tzinfo=ET)
    assert should_eod_exit(now, settings) is False


def test_should_eod_exit_uses_now_wall_clock_not_system_tz() -> None:
    # The check uses the wall-clock hour/minute of the passed datetime.
    # 10:00 ET (well before threshold) should not trigger an exit even
    # when the host system clock is in a different timezone.
    settings = SessionSettings()
    now = datetime(2026, 6, 18, 10, 0, tzinfo=ET)
    assert should_eod_exit(now, settings) is False
