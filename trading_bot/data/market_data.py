from __future__ import annotations

import pandas as pd


def normalize_ohlcv_frame(frame: pd.DataFrame) -> pd.DataFrame:
    renamed = frame.rename(
        columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        }
    ).reset_index(names="timestamp")
    return renamed[["timestamp", "open", "high", "low", "close", "volume"]]


def fetch_bars(symbol: str, period: str, interval: str) -> pd.DataFrame:
    from trading_bot.data.providers.yfinance_provider import YFinanceProvider

    return YFinanceProvider().fetch_bars(symbol, period, interval)
