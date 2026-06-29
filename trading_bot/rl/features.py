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

CROSS_SYMBOL_FEATURES = [
    "relative_strength_5d",
    "relative_strength_20d",
    "correlation_avg",
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


def build_cross_symbol_features(
    symbol: str,
    symbol_frames: dict[str, pd.DataFrame],
    index: int,
) -> list[float]:
    """Compute cross-symbol features for a single symbol.

    Returns [relative_strength_5d, relative_strength_20d, correlation_avg].
    
    relative_strength: how much this symbol outperformed the portfolio average
    correlation_avg: rolling correlation with other symbols in the portfolio
    """
    if index < 20 or symbol not in symbol_frames:
        return [0.0, 0.0, 0.0]

    symbol_df = symbol_frames[symbol]
    if index >= len(symbol_df):
        return [0.0, 0.0, 0.0]

    symbol_close = symbol_df["close"].iloc[: index + 1]

    other_symbols = [s for s in symbol_frames if s != symbol]
    if not other_symbols:
        return [0.0, 0.0, 0.0]

    symbol_ret_5d = _safe_returns(symbol_close, 5)
    symbol_ret_20d = _safe_returns(symbol_close, 20)

    other_rets_5d = []
    other_rets_20d = []
    other_closes = []

    for other_sym in other_symbols:
        other_df = symbol_frames[other_sym]
        if index >= len(other_df):
            continue
        other_close = other_df["close"].iloc[: index + 1]
        other_ret_5d = _safe_returns(other_close, 5)
        other_ret_20d = _safe_returns(other_close, 20)
        other_rets_5d.append(other_ret_5d)
        other_rets_20d.append(other_ret_20d)
        other_closes.append(other_close)

    avg_ret_5d = np.mean(other_rets_5d) if other_rets_5d else 0.0
    avg_ret_20d = np.mean(other_rets_20d) if other_rets_20d else 0.0

    rel_strength_5d = symbol_ret_5d - avg_ret_5d
    rel_strength_20d = symbol_ret_20d - avg_ret_20d

    if len(other_closes) < 2:
        corr_avg = 0.0
    else:
        corr_values = []
        for other_close in other_closes:
            min_len = min(len(symbol_close), len(other_close))
            if min_len < 20:
                continue
            sym_returns = symbol_close.iloc[-min_len:].pct_change().iloc[20:]
            other_returns = other_close.iloc[-min_len:].pct_change().iloc[20:]
            if len(sym_returns) < 10 or len(other_returns) < 10:
                continue
            corr = sym_returns.corr(other_returns)
            if pd.notna(corr):
                corr_values.append(corr)
        corr_avg = np.mean(corr_values) if corr_values else 0.0

    return [
        float(rel_strength_5d),
        float(rel_strength_20d),
        float(corr_avg),
    ]


def _safe_returns(close_series: pd.Series, periods: int) -> float:
    if len(close_series) < periods + 1:
        return 0.0
    prev = close_series.iloc[-periods - 1]
    curr = close_series.iloc[-1]
    if prev <= 0:
        return 0.0
    return (curr - prev) / prev


def build_observation(
    market_rows: list[list[float]],
    portfolio_row: list[float],
    *,
    observer_window: int,
    data_frames: dict[str, Any] | None = None,
    data_indices: dict[str, int] | None = None,
    symbol_frames: dict[str, Any] | None = None,
    n_market_features: int = len(FEATURE_COLS),
    n_cross_features: int = len(CROSS_SYMBOL_FEATURES),
    symbols: list[str] | None = None,
) -> np.ndarray:
    if data_frames is not None and data_indices is not None and symbols is not None:
        history = []
        max_idx = max(data_indices.values()) if data_indices else 0
        for offset in range(observer_window):
            idx = max_idx - observer_window + 1 + offset
            if idx < 0:
                history.append([0.0] * (len(symbols) * (n_market_features + n_cross_features) + len(portfolio_row)))
                continue
            row = []
            for symbol in symbols:
                df = data_frames.get(symbol)
                if df is not None and not df.empty and idx < len(df):
                    sliced = df.iloc[:idx + 1]
                    market_feat = build_market_feature_row(sliced)
                    cross_feat = build_cross_symbol_features(symbol, symbol_frames or data_frames, idx)
                    row.extend(market_feat)
                    row.extend(cross_feat)
                else:
                    row.extend([0.0] * (n_market_features + n_cross_features))
            row.extend(float(value) for value in portfolio_row)
            history.append(row)
        while len(history) < observer_window:
            history.insert(0, [0.0] * (len(symbols) * (n_market_features + n_cross_features) + len(portfolio_row)))
        return np.array(history[:observer_window], dtype=np.float32)
    
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
