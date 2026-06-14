from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def is_bullish_daily_regime(frame: "pd.DataFrame") -> bool:
    latest = frame.iloc[-1]
    return bool(latest["close"] > latest["ema_20"] > latest["sma_50"])
