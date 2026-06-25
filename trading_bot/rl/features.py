from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from trading_bot.data.indicators import (
    add_atr_percent,
    add_bollinger_bands,
    add_ema,
    add_macd,
    add_rsi,
    add_sma,
)
from trading_bot.models.portfolio import PortfolioState

FEATURE_COLS = [
    "close",
    "return_1d",
    "rsi_14",
    "ema_12",
    "ema_26",
    "sma_20",
    "macd_line",
    "macd_signal",
    "macd_histogram",
    "bb_percent_b",
    "bb_width",
    "atr_pct",
    "volume_ratio",
]

PORTFOLIO_FEATURES = [
    "cash_ratio",
    "num_positions",
    "position_weight_sum",
    "unrealized_pnl_pct",
    "realized_pnl_pct",
]


def build_market_feature_frame(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    result = add_ema(result, 12, "ema_12")
    result = add_ema(result, 26, "ema_26")
    result = add_rsi(result, 14)
    result = add_sma(result, 20, "sma_20")
    result = add_macd(result, 12, 26, 9)
    result = add_bollinger_bands(result, 20, 2.0)
    result = add_atr_percent(result, 14)
    return result


def build_market_feature_row(frame: pd.DataFrame) -> list[float]:
    if frame.empty or "close" not in frame.columns:
        return [0.0] * len(FEATURE_COLS)

    df = build_market_feature_frame(frame)
    close_col = _finite_float(df["close"].iloc[-1], default=0.0)
    returns = 0.0
    if len(df) >= 2:
        prev_close = _finite_float(df["close"].iloc[-2], default=0.0)
        if prev_close > 0:
            returns = (close_col - prev_close) / prev_close

    volume_ratio = 1.0
    if len(df) >= 2 and "volume" in df.columns:
        prev_vol = _finite_float(df["volume"].iloc[-2], default=0.0)
        volume = _finite_float(df["volume"].iloc[-1], default=0.0)
        if prev_vol > 0:
            volume_ratio = volume / prev_vol

    return [
        close_col,
        returns,
        _finite_float(df.get("rsi_14", pd.Series([50.0])).iloc[-1], default=50.0),
        _finite_float(df.get("ema_12", pd.Series([close_col])).iloc[-1], default=close_col),
        _finite_float(df.get("ema_26", pd.Series([close_col])).iloc[-1], default=close_col),
        _finite_float(df.get("sma_20", pd.Series([close_col])).iloc[-1], default=close_col),
        _finite_float(df.get("macd_line", pd.Series([0.0])).iloc[-1], default=0.0),
        _finite_float(df.get("macd_signal", pd.Series([0.0])).iloc[-1], default=0.0),
        _finite_float(df.get("macd_histogram", pd.Series([0.0])).iloc[-1], default=0.0),
        _finite_float(df.get("bb_percent_b", pd.Series([50.0])).iloc[-1], default=50.0),
        _finite_float(df.get("bb_width", pd.Series([0.0])).iloc[-1], default=0.0),
        _finite_float(df.get("atr_pct", pd.Series([0.0])).iloc[-1], default=0.0),
        volume_ratio,
    ]


def build_portfolio_feature_row(state: PortfolioState) -> list[float]:
    equity = max(state.equity, 1e-8)
    position_weight_sum = sum(
        position.quantity * position.average_cost / equity
        for position in state.positions.values()
    )
    return [
        float(state.cash / equity),
        float(len(state.positions)),
        float(position_weight_sum),
        float(state.unrealized_pnl / equity),
        float(state.realized_pnl / equity),
    ]


def build_observation(
    market_rows: list[list[float]],
    portfolio_row: list[float],
    *,
    observer_window: int,
) -> np.ndarray:
    row = [float(value) for row_values in market_rows for value in row_values]
    row.extend(float(value) for value in portfolio_row)
    history = [row]
    zero_row = [0.0] * len(row)
    while len(history) < observer_window:
        history.insert(0, zero_row)
    return np.array(history[:observer_window], dtype=np.float32)


def pad_market_rows(market_rows: list[list[float]], max_symbols: int) -> list[list[float]]:
    padded = [list(row) for row in market_rows[:max_symbols]]
    while len(padded) < max_symbols:
        padded.append([0.0] * len(FEATURE_COLS))
    return padded


def _finite_float(value: Any, *, default: float) -> float:
    try:
        if pd.notna(value):
            return float(value)
    except Exception:
        pass
    return default
