from __future__ import annotations

import pandas as pd

from trading_bot.rl.labels import add_future_direction_label, add_future_return_pct


def test_add_future_return_pct_marks_tail_unknown() -> None:
    frame = pd.DataFrame({"close": [100.0, 105.0, 110.0]})

    result = add_future_return_pct(frame, horizon=1)

    assert round(result["future_return_1"].iloc[0], 2) == 5.0
    assert round(result["future_return_1"].iloc[1], 2) == 4.76
    assert pd.isna(result["future_return_1"].iloc[2])


def test_add_future_direction_label_has_no_future_leakage() -> None:
    frame = pd.DataFrame({"close": [100.0, 101.0, 99.0]})

    result = add_future_direction_label(frame, horizon=1, threshold_pct=0.5)

    assert result["future_up_1"].iloc[0] == 1.0
    assert result["future_up_1"].iloc[1] == 0.0
    assert pd.isna(result["future_up_1"].iloc[2])
