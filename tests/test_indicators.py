from __future__ import annotations

import pytest

from trading_bot.data.indicators import add_ema_column, add_rsi_column


def test_add_ema_column_creates_expected_values() -> None:
    rows = [{"close": price} for price in [10.0, 11.0, 12.0, 13.0, 14.0]]

    result = add_ema_column(rows, period=3, price_key="close", column_name="ema_3")

    assert "ema_3" not in rows[0]
    assert [row["ema_3"] for row in result[:2]] == [None, None]
    assert result[2]["ema_3"] == pytest.approx(11.0)
    assert result[3]["ema_3"] == pytest.approx(12.0)
    assert result[4]["ema_3"] == pytest.approx(13.0)


def test_add_rsi_column_keeps_values_bounded() -> None:
    rows = [{"close": price} for price in [44.0, 44.15, 43.9, 44.35, 44.8, 44.5, 45.0, 45.4, 45.1]]

    result = add_rsi_column(rows, period=3, price_key="close", column_name="rsi_3")

    bounded_values = [value for value in (row["rsi_3"] for row in result) if value is not None]

    assert bounded_values
    assert all(0.0 <= value <= 100.0 for value in bounded_values)
