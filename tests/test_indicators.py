from __future__ import annotations

import pytest
import pandas as pd

from trading_bot.data.indicators import add_ema, add_rsi


def test_add_ema_creates_expected_column() -> None:
    frame = pd.DataFrame({"close": [10.0, 11.0, 12.0, 13.0, 14.0]})

    result = add_ema(frame, period=3, column_name="ema_3")

    assert "ema_3" not in frame.columns
    assert "ema_3" in result.columns
    assert result["ema_3"].iloc[:2].isna().all()
    assert result["ema_3"].iloc[2] == pytest.approx(11.0)
    assert result["ema_3"].iloc[3] == pytest.approx(12.0)
    assert result["ema_3"].iloc[4] == pytest.approx(13.0)


def test_add_rsi_keeps_values_bounded() -> None:
    frame = pd.DataFrame(
        {"close": [44.0, 44.15, 43.9, 44.35, 44.8, 44.5, 45.0, 45.4, 45.1]}
    )

    result = add_rsi(frame, period=3)

    bounded_values = result["rsi_3"].dropna()

    assert bounded_values
    assert bounded_values.between(0.0, 100.0).all()
