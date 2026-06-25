from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import pandas as pd


class GroupByScaler:
    """Per-ticker rolling normalization (z-score and min-max).

    Mirrors FinRL's GroupByScaler pattern: each ticker maintains its own
    rolling statistics so that features are normalized relative to that
    ticker's own history rather than a global cross-sectional distribution.

    Usage::

        scaler = GroupByScaler(window=60)
        for ticker, frame in frames_by_ticker.items():
            normalized = scaler.fit_transform(frame, ticker)
    """

    def __init__(
        self,
        window: int = 60,
        method: str = "zscore",
        min_max_low: float = 0.0,
        min_max_high: float = 1.0,
    ) -> None:
        self.window = window
        self.method = method
        self.min_max_low = min_max_low
        self.min_max_high = min_max_high
        self._history: dict[str, list[float]] = defaultdict(list)

    def fit_transform(self, values: list[float | None], ticker: str) -> np.ndarray:
        """Normalize a list of (possibly NaN) values for a single ticker.

        Returns a numpy array of the same length with normalized values.
        NaN / None inputs produce NaN outputs.
        """
        result = np.empty(len(values), dtype=np.float64)
        history = self._history[ticker]

        for i, v in enumerate(values):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                result[i] = np.nan
                continue

            fv = float(v)
            history.append(fv)
            if len(history) > self.window:
                history.pop(0)

            if self.method == "zscore":
                result[i] = self._zscore_normalize(fv, history)
            elif self.method == "minmax":
                result[i] = self._minmax_normalize(fv, history)
            else:
                result[i] = fv

        return result

    def _zscore_normalize(self, value: float, history: list[float]) -> float:
        n = len(history)
        if n < 2:
            return 0.0
        mean = sum(history) / n
        variance = sum((x - mean) ** 2 for x in history) / n
        std = math.sqrt(variance) if variance > 0 else 1e-8
        return (value - mean) / std

    def _minmax_normalize(self, value: float, history: list[float]) -> float:
        n = len(history)
        if n < 1:
            return 0.5
        lo = min(history)
        hi = max(history)
        if hi == lo:
            return (self.min_max_low + self.min_max_high) / 2.0
        return self.min_max_low + (value - lo) / (hi - lo) * (self.min_max_high - self.min_max_low)


class FeatureEngineer:
    """Computes RL-friendly feature vectors from OHLCV + indicator data.

    Builds a state vector from:
    - Price features (log returns, price vs MAs, Bollinger %B)
    - Momentum indicators (RSI, MACD, Stochastic, CCI, Williams %R)
    - Volatility indicators (ATR, ADX)
    - Volume features (volume ratio, OBV)
    - Portfolio state (position weight, unrealized PnL, cash ratio)

    Usage::

        engineer = FeatureEngineer()
        state = engineer.build_state(
            daily_frame=frame,
            ticker="AAPL",
            portfolio_weight=0.15,
            unrealized_pnl_pct=0.02,
            cash_ratio=0.85,
        )
        # state is a numpy array ready for RL agent input
    """

    STANDARD_FEATURES = [
        "log_return_1d",
        "log_return_5d",
        "price_vs_ema20",
        "price_vs_sma50",
        "bb_percent_b",
        "rsi_14",
        "macd_line",
        "macd_histogram",
        "stoch_k",
        "cci",
        "williams_r",
        "atr_pct",
        "adx",
        "volume_ratio",
        "obv_change",
        "vwap_distance",
    ]

    EXTENDED_FEATURES = STANDARD_FEATURES + [
        "rsi_7",
        "stoch_d",
        "plus_di",
        "minus_di",
        "bb_width",
    ]

    def __init__(self, feature_set: str = "standard") -> None:
        self.feature_set = feature_set
        self.feature_names = (
            self.STANDARD_FEATURES
            if feature_set == "standard"
            else self.EXTENDED_FEATURES
        )
        self.scaler = GroupByScaler(window=60, method="zscore")

    def build_state(
        self,
        frame: "pd.DataFrame",
        ticker: str = "",
        portfolio_weight: float = 0.0,
        unrealized_pnl_pct: float = 0.0,
        cash_ratio: float = 1.0,
    ) -> np.ndarray:
        """Build a normalized feature vector from a single OHLCV frame.

        Args:
            frame: OHLCV DataFrame with columns [open, high, low, close, volume].
                   May also have indicator columns pre-computed.
            ticker: Ticker symbol for per-ticker normalization.
            portfolio_weight: Current position weight (0-1).
            unrealized_pnl_pct: Current unrealized PnL as percentage.
            cash_ratio: Fraction of portfolio in cash (0-1).

        Returns:
            numpy array of normalized features ready for RL agent input.
        """
        raw_features = self._extract_raw_features(frame)
        portfolio_features = self._extract_portfolio_features(
            portfolio_weight, unrealized_pnl_pct, cash_ratio
        )
        all_raw = raw_features + portfolio_features

        normalized = self.scaler.fit_transform(all_raw, ticker if ticker else "global")
        return normalized

    def _extract_raw_features(self, frame: "pd.DataFrame") -> list[float | None]:
        """Extract raw (unnormalized) feature values from OHLCV frame."""
        closes = [float(v) for v in frame["close"].tolist()]
        volumes = [float(v) for v in frame["volume"].tolist()]
        n = len(closes)
        if n == 0:
            return [None] * len(self.feature_names)

        features: list[float | None] = []

        # Log returns
        features.append(self._log_return(closes, 1))
        features.append(self._log_return(closes, 5))

        # Price vs MAs
        ema20 = self._ema_values(closes, 20)
        sma50 = self._sma_values(closes, 50)
        features.append(self._price_vs_ma(closes, ema20))
        features.append(self._price_vs_ma(closes, sma50))

        # Bollinger %B
        bb_pct_b = self._extract_column(frame, "bb_percent_b", n)
        features.append(bb_pct_b[-1] if bb_pct_b and bb_pct_b[-1] is not None else None)

        # RSI
        rsi14 = self._extract_column(frame, "rsi_14", n)
        features.append(rsi14[-1] if rsi14 and rsi14[-1] is not None else None)

        if self.feature_set == "extended":
            rsi7 = self._extract_column(frame, "rsi_7", n)
            features.append(rsi7[-1] if rsi7 and rsi7[-1] is not None else None)

        # MACD
        macd_line = self._extract_column(frame, "macd_line", n)
        macd_hist = self._extract_column(frame, "macd_histogram", n)
        features.append(macd_line[-1] if macd_line and macd_line[-1] is not None else None)
        features.append(macd_hist[-1] if macd_hist and macd_hist[-1] is not None else None)

        # Stochastic
        stoch_k = self._extract_column(frame, "stoch_k", n)
        features.append(stoch_k[-1] if stoch_k and stoch_k[-1] is not None else None)

        if self.feature_set == "extended":
            stoch_d = self._extract_column(frame, "stoch_d", n)
            features.append(stoch_d[-1] if stoch_d and stoch_d[-1] is not None else None)

        # CCI
        cci = self._extract_column(frame, "cci", n)
        features.append(cci[-1] if cci and cci[-1] is not None else None)

        # Williams %R
        wr = self._extract_column(frame, "williams_r", n)
        features.append(wr[-1] if wr and wr[-1] is not None else None)

        # ATR %
        atr_pct = self._extract_column(frame, "atr_pct", n)
        features.append(atr_pct[-1] if atr_pct and atr_pct[-1] is not None else None)

        # ADX
        adx = self._extract_column(frame, "adx", n)
        features.append(adx[-1] if adx and adx[-1] is not None else None)

        if self.feature_set == "extended":
            features.append(self._extract_di(frame, "plus_di", n, -1))
            features.append(self._extract_di(frame, "minus_di", n, -1))
            features.append(self._extract_column(frame, "bb_width", n)[-1] if self._extract_column(frame, "bb_width", n) else None)

        # Volume ratio (current / 5-day avg)
        features.append(self._volume_ratio(volumes))

        # OBV change
        obv = self._extract_column(frame, "obv", n)
        features.append(self._obv_change(obv))

        # VWAP distance
        vwap = self._extract_column(frame, "vwap", n)
        features.append(self._price_vs_vwap(closes, vwap))

        return features

    def _extract_portfolio_features(
        self,
        portfolio_weight: float,
        unrealized_pnl_pct: float,
        cash_ratio: float,
    ) -> list[float]:
        """Extract portfolio state features (not normalized per-ticker)."""
        return [
            portfolio_weight,
            unrealized_pnl_pct,
            cash_ratio,
        ]

    # --- Helper methods ---

    @staticmethod
    def _log_return(prices: list[float], period: int) -> float | None:
        if len(prices) < period + 1:
            return None
        prev = prices[-(period + 1)]
        curr = prices[-1]
        if prev <= 0:
            return None
        return math.log(curr / prev)

    @staticmethod
    def _ema_values(prices: list[float], period: int) -> list[float | None]:
        result: list[float | None] = [None] * len(prices)
        if len(prices) < period:
            return result
        multiplier = 2.0 / (period + 1.0)
        current = sum(prices[:period]) / period
        result[period - 1] = current
        for i in range(period, len(prices)):
            current = (prices[i] - current) * multiplier + current
            result[i] = current
        return result

    @staticmethod
    def _sma_values(prices: list[float], period: int) -> list[float | None]:
        result: list[float | None] = [None] * len(prices)
        if len(prices) < period:
            return result
        running_total = sum(prices[:period])
        result[period - 1] = running_total / period
        for i in range(period, len(prices)):
            running_total += prices[i] - prices[i - period]
            result[i] = running_total / period
        return result

    @staticmethod
    def _price_vs_ma(prices: list[float], ma_values: list[float | None]) -> float | None:
        if not prices or not ma_values:
            return None
        last_price = prices[-1]
        last_ma = ma_values[-1]
        if last_ma is None or last_ma == 0:
            return None
        return (last_price - last_ma) / last_ma

    @staticmethod
    def _extract_column(frame: "pd.DataFrame", col: str, n: int) -> list[float | None]:
        if col not in frame.columns:
            return [None] * n
        values = frame[col].tolist()
        return [float(v) if v is not None else None for v in values]

    @staticmethod
    def _extract_di(frame: "pd.DataFrame", col: str, n: int, idx: int) -> float | None:
        if col not in frame.columns:
            return None
        values = frame[col].tolist()
        return float(values[idx]) if values[idx] is not None else None

    def _volume_ratio(self, volumes: list[float]) -> float | None:
        if len(volumes) < 6:
            return None
        current = volumes[-1]
        avg = sum(volumes[-6:-1]) / 5
        if avg == 0:
            return None
        return current / avg

    @staticmethod
    def _obv_change(obv_values: list[float | None] | None) -> float | None:
        if not obv_values or len(obv_values) < 3:
            return None
        last = obv_values[-1]
        prev = obv_values[-3]
        if last is None or prev is None or prev == 0:
            return None
        return (last - prev) / abs(prev)

    @staticmethod
    def _price_vs_vwap(prices: list[float], vwap_values: list[float | None]) -> float | None:
        if not prices or not vwap_values:
            return None
        last_price = prices[-1]
        last_vwap = vwap_values[-1]
        if last_vwap is None or last_vwap == 0:
            return None
        return (last_price - last_vwap) / last_vwap

    @property
    def state_size(self) -> int:
        """Total number of features (raw + portfolio state)."""
        return len(self.feature_names) + 3  # 3 portfolio features
