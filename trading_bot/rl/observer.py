from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from typing import Any

import gymnasium as gym
import numpy as np
import pandas as pd

from trading_bot.models.portfolio import PortfolioState


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

    FEATURE_COLS = [
        "close", "return_1d", "rsi_14", "ema_12", "ema_26",
        "sma_20", "macd_line", "macd_signal", "macd_histogram",
        "bb_percent_b", "bb_width", "atr_pct", "volume_ratio",
    ]

    PORTFOLIO_FEATURES = [
        "cash_ratio", "num_positions", "position_weight_sum",
        "unrealized_pnl_pct", "realized_pnl_pct",
    ]

    def __init__(
        self,
        symbols: list[str],
        window_size: int = 10,
        period: str = "1y",
        interval: str = "1d",
    ) -> None:
        self.symbols = [s.upper().strip() for s in symbols]
        self.window_size = window_size
        self.period = period
        self.interval = interval
        self.n_market_features = len(self.FEATURE_COLS)
        self.n_portfolio_features = len(self.PORTFOLIO_FEATURES)
        self.n_symbols = len(symbols)
        self.n_features = self.n_symbols * self.n_market_features + self.n_portfolio_features
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
        from trading_bot.data.indicators import (
            add_ema, add_rsi, add_sma, add_atr, add_macd,
            add_bollinger_bands, add_atr_percent,
        )

        try:
            from trading_bot.data.market_data import fetch_bars
            df = fetch_bars(symbol, self.period, self.interval)
        except Exception:
            return [0.0] * self.n_market_features

        if df is None or df.empty or "close" not in df.columns:
            return [0.0] * self.n_market_features

        try:
            df = add_ema(df, 12, "ema_12")
            df = add_ema(df, 26, "ema_26")
            df = add_rsi(df, 14)
            df = add_sma(df, 20, "sma_20")
            df = add_macd(df, 12, 26, 9)
            df = add_bollinger_bands(df, 20, 2.0)
            df = add_atr_percent(df, 14)

            close_col = df["close"].iloc[-1]
            if pd.isna(close_col):
                close_col = 0.0

            returns = 0.0
            if len(df) >= 2:
                prev_close = df["close"].iloc[-2]
                if pd.notna(prev_close) and prev_close > 0:
                    returns = (close_col - prev_close) / prev_close

            rsi = df["rsi_14"].iloc[-1] if "rsi_14" in df.columns else 50.0
            ema_12 = df["ema_12"].iloc[-1] if "ema_12" in df.columns else close_col
            ema_26 = df["ema_26"].iloc[-1] if "ema_26" in df.columns else close_col
            sma_20 = df["sma_20"].iloc[-1] if "sma_20" in df.columns else close_col

            macd_line = df["macd_line"].iloc[-1] if "macd_line" in df.columns else 0.0
            macd_signal = df["macd_signal"].iloc[-1] if "macd_signal" in df.columns else 0.0
            macd_hist = df["macd_histogram"].iloc[-1] if "macd_histogram" in df.columns else 0.0

            bb_pct = df["bb_percent_b"].iloc[-1] if "bb_percent_b" in df.columns else 50.0
            bb_w = df["bb_width"].iloc[-1] if "bb_width" in df.columns else 0.0
            atr_pct = df["atr_pct"].iloc[-1] if "atr_pct" in df.columns else 0.0

            volume = df["volume"].iloc[-1] if "volume" in df.columns else 0.0
            volume_ratio = 1.0
            if len(df) >= 2 and "volume" in df.columns:
                prev_vol = df["volume"].iloc[-2]
                if pd.notna(prev_vol) and prev_vol > 0:
                    volume_ratio = volume / prev_vol

            return [
                float(close_col),
                float(returns),
                float(rsi),
                float(ema_12),
                float(ema_26),
                float(sma_20),
                float(macd_line),
                float(macd_signal),
                float(macd_hist),
                float(bb_pct),
                float(bb_w),
                float(atr_pct),
                float(volume_ratio),
            ]
        except Exception:
            return [0.0] * self.n_market_features

    def _compute_portfolio_features(self, state: PortfolioState) -> list[float]:
        equity = max(state.equity, 1e-8)
        cash_ratio = state.cash / equity
        num_positions = len(state.positions)
        position_weight_sum = sum(
            p.quantity * p.average_cost / equity
            for p in state.positions.values()
        )
        unrealized_pnl_pct = state.unrealized_pnl / equity
        realized_pnl_pct = state.realized_pnl / equity

        return [
            float(cash_ratio),
            float(num_positions),
            float(position_weight_sum),
            float(unrealized_pnl_pct),
            float(realized_pnl_pct),
        ]

    def observe(
        self,
        portfolio_state: PortfolioState,
        prices: dict[str, float],
        step: int,
    ) -> np.ndarray:
        market_features = []
        for symbol in self.symbols:
            features = self._load_and_compute_features(symbol)
            market_features.extend(features)

        portfolio_features = self._compute_portfolio_features(portfolio_state)
        all_features = market_features + portfolio_features

        row = [float(v) for v in all_features]
        self._history.append(row)

        history_list = list(self._history)
        if len(history_list) < self.window_size:
            padding_rows = self.window_size - len(history_list)
            zero_row = [0.0] * self.n_features
            for _ in range(padding_rows):
                history_list.insert(0, zero_row)

        return np.array(history_list[:self.window_size], dtype=np.float32)
