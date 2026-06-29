from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd

from trading_bot.models.portfolio import PortfolioState
from trading_bot.rl.features import (
    CROSS_SYMBOL_FEATURES,
    FEATURE_COLS,
    PORTFOLIO_FEATURES,
    build_cross_symbol_features,
    build_market_feature_row,
    build_portfolio_feature_row,
    pad_market_rows,
)


class Observer(ABC):
    def __init__(self) -> None:
        self._observation_space: gym.spaces.Space | None = None

    @property
    def observation_space(self) -> gym.spaces.Space:
        if self._observation_space is None:
            raise NotImplementedError("observation_space not initialized")
        return self._observation_space

    @abstractmethod
    def observe(
        self,
        portfolio_state: PortfolioState,
        prices: dict[str, float],
        step: int,
    ) -> np.ndarray: ...

    def reset(self) -> None:
        pass


class TensorTradeObserver(Observer):
    """Windowed observer combining market features + portfolio state.

    Observation shape: (window_size, n_features)

    Features per bar:
    - Price: close, returns, volume
    - Technical: RSI(14), EMA(12), EMA(26), SMA(20), MACD, BB%, ATR%
    - Portfolio: cash_ratio, num_positions, position_weights, unrealized_pnl_pct

    Pads initial steps with zeros until window is filled.
    """

    FEATURE_COLS = FEATURE_COLS
    CROSS_SYMBOL_FEATURES = CROSS_SYMBOL_FEATURES
    PORTFOLIO_FEATURES = PORTFOLIO_FEATURES

    def __init__(
        self,
        symbols: list[str],
        window_size: int = 10,
        period: str = "1y",
        interval: str = "1d",
        max_symbols: int | None = None,
    ) -> None:
        self.symbols = [s.upper().strip() for s in symbols]
        self.window_size = window_size
        self.period = period
        self.interval = interval
        self.n_market_features = len(self.FEATURE_COLS)
        self.n_cross_features = len(self.CROSS_SYMBOL_FEATURES)
        self.n_portfolio_features = len(self.PORTFOLIO_FEATURES)
        self.n_symbols = len(symbols)
        # Fixed-size observation space allows symbol-agnostic inference/training.
        # If max_symbols is set, observations are padded/truncated to that size.
        self.max_symbols = max_symbols if max_symbols else self.n_symbols
        if self.max_symbols < self.n_symbols:
            raise ValueError(
                f"max_symbols ({self.max_symbols}) must be >= number of symbols ({self.n_symbols})"
            )
        self.n_features = self.max_symbols * (self.n_market_features + self.n_cross_features) + self.n_portfolio_features
        self._observation_space = gym.spaces.Box(
            low=-np.inf,
            high=np.inf,
            shape=(window_size, self.n_features),
            dtype=np.float32,
        )
        self._history: deque[list[float]] = deque(maxlen=window_size)
        self._symbols_loaded = False

    def reset(self) -> None:
        self._history.clear()

    def _load_and_compute_features(self, symbol: str) -> list[float]:
        try:
            from trading_bot.data.market_data import fetch_bars
            df = fetch_bars(symbol, self.period, self.interval)
        except Exception:
            return [0.0] * self.n_market_features

        if df is None:
            return [0.0] * self.n_market_features

        try:
            return build_market_feature_row(df)
        except Exception:
            return [0.0] * self.n_market_features

    def _compute_portfolio_features(self, state: PortfolioState) -> list[float]:
        return build_portfolio_feature_row(state)

    def observe(
        self,
        portfolio_state: PortfolioState,
        prices: dict[str, float],
        step: int,
        data_frames: dict[str, pd.DataFrame] | None = None,
        data_indices: dict[str, int] | None = None,
        symbol_frames: dict[str, pd.DataFrame] | None = None,
    ) -> np.ndarray:
        if data_frames is not None and data_indices is not None:
            market_rows = []
            cross_rows = []
            for symbol in self.symbols:
                df = data_frames.get(symbol)
                idx = data_indices.get(symbol, 0)
                if df is not None and not df.empty and idx < len(df):
                    sliced = df.iloc[:idx + 1]
                    market_rows.append(build_market_feature_row(sliced))
                else:
                    market_rows.append([0.0] * self.n_market_features)
                
                cross_df = symbol_frames if symbol_frames is not None else data_frames
                if cross_df is not None:
                    cross_feat = build_cross_symbol_features(symbol, cross_df, idx)
                    cross_rows.append(cross_feat)
                else:
                    cross_rows.append([0.0] * self.n_cross_features)
        else:
            market_rows = [self._load_and_compute_features(symbol) for symbol in self.symbols]
            cross_rows = [[0.0] * self.n_cross_features for _ in self.symbols]
        
        market_rows = pad_market_rows(market_rows, self.max_symbols)
        cross_rows = pad_market_rows(cross_rows, self.max_symbols)
        portfolio_features = self._compute_portfolio_features(portfolio_state)
        
        row = []
        for m_row, c_row in zip(market_rows, cross_rows):
            row.extend(m_row)
            row.extend(c_row)
        row.extend(float(value) for value in portfolio_features)
        self._history.append(row)

        history_list = list(self._history)
        if len(history_list) < self.window_size:
            zero_row = [0.0] * self.n_features
            while len(history_list) < self.window_size:
                history_list.insert(0, zero_row)
        return np.array(history_list[:self.window_size], dtype=np.float32)
