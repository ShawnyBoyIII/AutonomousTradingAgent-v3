from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from trading_bot.data.providers.alpaca_provider import (
    _normalize_symbol,
    _parse_interval,
    _period_to_start_end,
    _split_period,
)


class TestNormalizeSymbol:
    """Share-class symbols use dashes in yfinance but dots in Alpaca."""

    def test_regular_symbol_unchanged(self) -> None:
        assert _normalize_symbol("AAPL") == "AAPL"
        assert _normalize_symbol("SPY") == "SPY"

    def test_share_class_dash_to_dot(self) -> None:
        assert _normalize_symbol("BRK-B") == "BRK.B"
        assert _normalize_symbol("BF-B") == "BF.B"
        assert _normalize_symbol("BIO-A") == "BIO.A"

    def test_no_dashes_no_change(self) -> None:
        assert _normalize_symbol("MSFT") == "MSFT"
        assert _normalize_symbol("GOOGL") == "GOOGL"


class TestSplitPeriod:
    def test_single_char_units(self) -> None:
        assert _split_period("5m") == (5, "m")
        assert _split_period("1d") == (1, "d")
        assert _split_period("2w") == (2, "w")
        assert _split_period("1y") == (1, "y")
        assert _split_period("1h") == (1, "h")

    def test_multi_char_month_unit(self) -> None:
        assert _split_period("1mo") == (1, "mo")
        assert _split_period("3mo") == (3, "mo")
        assert _split_period("6mo") == (6, "mo")

    def test_double_digit_value(self) -> None:
        assert _split_period("15m") == (15, "m")
        assert _split_period("12mo") == (12, "mo")

    def test_month_not_misread_as_minutes(self) -> None:
        # Regression: "1mo" previously took [-1] -> "o", value=int("1m") -> ValueError
        value, unit = _split_period("1mo")
        assert unit == "mo"
        assert value == 1


class TestParseInterval:
    @pytest.mark.parametrize(
        "interval,expected",
        [
            ("5m", (5, "Minute")),
            ("15m", (15, "Minute")),
            ("1h", (1, "Hour")),
            ("1d", (1, "Day")),
            ("1mo", (1, "Month")),
            ("3mo", (3, "Month")),
        ],
    )
    def test_known_intervals(self, interval: str, expected: tuple[int, str]) -> None:
        assert _parse_interval(interval) == expected

    def test_unknown_unit_defaults_to_day(self) -> None:
        assert _parse_interval("2x") == (2, "Day")

    def test_month_interval_does_not_raise(self) -> None:
        # Regression: "1mo" previously raised ValueError on int("1m")
        assert _parse_interval("1mo") == (1, "Month")


class TestPeriodToStartEnd:
    def test_returns_end_near_now(self) -> None:
        before = datetime.now()
        start, end = _period_to_start_end("1mo")
        after = datetime.now()
        assert before <= end <= after

    def test_one_year_approx_365_days(self) -> None:
        start, end = _period_to_start_end("1y")
        delta = end - start
        assert timedelta(days=364) <= delta <= timedelta(days=366)

    def test_one_day(self) -> None:
        start, end = _period_to_start_end("1d")
        assert end - start == timedelta(days=1)

    def test_two_weeks(self) -> None:
        start, end = _period_to_start_end("2w")
        assert end - start == timedelta(weeks=2)

    @pytest.mark.parametrize("period,approx_days", [("1mo", 30), ("3mo", 90), ("6mo", 180)])
    def test_month_periods(self, period: str, approx_days: int) -> None:
        start, end = _period_to_start_end(period)
        delta = end - start
        assert delta == timedelta(days=approx_days)

    def test_one_month_does_not_raise(self) -> None:
        # Regression: "1mo" previously raised ValueError on int("1m")
        start, end = _period_to_start_end("1mo")
        assert end > start
