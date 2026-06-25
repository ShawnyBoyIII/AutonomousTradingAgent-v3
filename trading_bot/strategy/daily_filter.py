from __future__ import annotations

import pandas as pd

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def is_bullish_daily_regime(frame: "pd.DataFrame") -> bool:
    required_columns = {"close", "ema_20", "sma_50"}
    if frame.empty or not required_columns.issubset(frame.columns):
        return False

    latest = frame.iloc[-1]
    close = latest["close"]
    ema20 = latest["ema_20"]
    sma50 = latest["sma_50"]
    
    # Check for valid (non-null) values
    if pd.isna(close) or pd.isna(ema20) or pd.isna(sma50):
        return False
    
    return bool(close > ema20 > sma50)
