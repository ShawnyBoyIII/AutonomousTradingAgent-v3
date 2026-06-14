from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def detect_intraday_breakout(frame: "pd.DataFrame", lookback: int = 4) -> bool:
    if lookback <= 0 or len(frame) <= lookback:
        return False

    latest = frame.iloc[-1]
    prior = frame.iloc[-(lookback + 1) : -1]
    range_high = prior["high"].max()
    return bool(latest["close"] > range_high and latest["volume"] > latest["volume_avg_5"])
