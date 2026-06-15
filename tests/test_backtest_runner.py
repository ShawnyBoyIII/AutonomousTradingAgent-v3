import pandas as pd
import pytest

from trading_bot.backtest.metrics import compute_win_rate
from trading_bot.backtest.runner import iterate_bars


def test_iterate_bars_yields_chronological_slices() -> None:
    frame = pd.DataFrame({"close": [1, 2, 3, 4]})
    slices = list(iterate_bars(frame, warmup=2))

    assert len(slices) == 2
    assert list(slices[0]["close"]) == [1, 2]
    assert list(slices[1]["close"]) == [1, 2, 3]


def test_iterate_bars_rejects_non_positive_warmup() -> None:
    frame = pd.DataFrame({"close": [1, 2, 3, 4]})

    with pytest.raises(ValueError, match="warmup must be positive"):
        list(iterate_bars(frame, warmup=0))


def test_compute_win_rate_returns_fraction_and_zero_safe_default() -> None:
    assert compute_win_rate(3, 1) == 0.75
    assert compute_win_rate(0, 0) == 0.0
