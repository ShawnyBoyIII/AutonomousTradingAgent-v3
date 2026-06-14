from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def detect_intraday_breakout(frame: "pd.DataFrame", lookback: int = 4) -> bool:
    required_columns = {"close", "high", "volume", "volume_avg_5"}
    if lookback <= 0 or len(frame) <= lookback or not required_columns.issubset(frame.columns):
        return False

    latest = frame.iloc[-1]
    prior = frame.iloc[-(lookback + 1) : -1]
    prior_highs_raw = [_to_finite_float(high) for high in prior["high"].tolist()]
    latest_close = _to_finite_float(latest["close"])
    latest_volume = _to_finite_float(latest["volume"])
    average_volume = _to_finite_float(latest["volume_avg_5"])

    if (
        any(value is None for value in prior_highs_raw)
        or latest_close is None
        or latest_volume is None
        or average_volume is None
    ):
        return False

    prior_highs = [value for value in prior_highs_raw if value is not None]
    range_high = max(prior_highs)
    return bool(latest_close > range_high and latest_volume > average_volume)


def _to_finite_float(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(numeric):
        return None

    return numeric
