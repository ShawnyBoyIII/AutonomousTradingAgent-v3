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
    indicator_cols = {
        "ema_12",
        "ema_26",
        "rsi_14",
        "sma_20",
        "macd_line",
        "macd_signal",
        "macd_histogram",
        "bb_percent_b",
        "bb_width",
        "atr_pct",
    }
    if indicator_cols.issubset(frame.columns):
        return frame.copy()

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

    ema_12_raw = _last_finite(df, "ema_12", close_col)
    ema_26_raw = _last_finite(df, "ema_26", close_col)
    sma_20_raw = _last_finite(df, "sma_20", close_col)
    macd_line_raw = _last_finite(df, "macd_line", 0.0)
    macd_signal_raw = _last_finite(df, "macd_signal", 0.0)
    macd_hist_raw = _last_finite(df, "macd_histogram", 0.0)
    bb_width_raw = _last_finite(df, "bb_width", 0.0)

    ema_12 = (ema_12_raw / close_col - 1.0) if close_col > 0 else 0.0
    ema_26 = (ema_26_raw / close_col - 1.0) if close_col > 0 else 0.0
    sma_20 = (sma_20_raw / close_col - 1.0) if close_col > 0 else 0.0
    macd_line = (macd_line_raw / close_col * 100.0) if close_col > 0 else 0.0
    macd_signal = (macd_signal_raw / close_col * 100.0) if close_col > 0 else 0.0
    macd_histogram = (macd_hist_raw / close_col * 100.0) if close_col > 0 else 0.0
    bb_width = (bb_width_raw / close_col * 100.0) if close_col > 0 else 0.0

    return [
        1.0,
        returns,
        _last_finite(df, "rsi_14", 50.0),
        ema_12,
        ema_26,
        sma_20,
        macd_line,
        macd_signal,
        macd_histogram,
        _last_finite(df, "bb_percent_b", 50.0),
        bb_width,
        _last_finite(df, "atr_pct", 0.0),
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


def _last_finite(df: pd.DataFrame, column: str, default: float) -> float:
    if column not in df.columns:
        return default
    return _finite_float(df[column].iloc[-1], default=default)
