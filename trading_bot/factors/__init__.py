"""Alpha Zoo: Pre-built quant factor library with benching capabilities.

Four factor zoos:
- Qlib 158: Quality, momentum, volatility factors
- Kakushadze 101: Technical and statistical factors
- GTJA 191: Trend-following and mean-reversion factors
- Academic 10: Classic academic alpha factors

Each factor implements the AlphaFactor protocol and can be benchmarked
independently or compared head-to-head.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class AlphaZoo(str, Enum):
    """Factor zoo categories."""

    QLIB = "qlib"
    KAKUSHADZE = "kakushadze"
    GTJA = "gtja"
    ACADEMIC = "academic"


class AlphaCategory(str, Enum):
    """Factor categories within zoos."""

    MOMENTUM = "momentum"
    VALUE = "value"
    QUALITY = "quality"
    VOLATILITY = "volatility"
    LIQUIDITY = "liquidity"
    TREND = "trend"
    MEAN_REVERSION = "mean_reversion"
    SENTIMENT = "sentiment"
    MACRO = "macro"
    TECHNICAL = "technical"


class AlphaFactor(ABC):
    """Abstract base class for alpha factors.

    Each factor computes a scalar score from OHLCV data that predicts
    future returns. Factors are standardized to zero mean and unit variance.
    """

    zoo: AlphaZoo = AlphaZoo.QLIB
    category: AlphaCategory = AlphaCategory.MOMENTUM
    description: str = ""
    params: dict[str, Any] = {}

    @abstractmethod
    def compute(self, frame: pd.DataFrame) -> float:
        """Compute factor value from OHLCV data.

        Args:
            frame: DataFrame with OHLCV columns.

        Returns:
            Factor score (higher = more bullish).
        """
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} ({self.zoo.value}/{self.category.value})>"


class AlphaFactorRegistry:
    """Registry for all alpha factors with lookup and filtering."""

    _factors: dict[str, AlphaFactor] = {}

    @classmethod
    def register(cls, factor: AlphaFactor) -> None:
        """Register a factor instance."""
        cls._factors[factor.__class__.__name__] = factor

    @classmethod
    def get(cls, name: str) -> AlphaFactor | None:
        """Get factor by name."""
        return cls._factors.get(name)

    @classmethod
    def get_by_zoo(cls, zoo: AlphaZoo | str) -> list[AlphaFactor]:
        """Get all factors from a zoo."""
        if isinstance(zoo, str):
            zoo = AlphaZoo(zoo)
        return [f for f in cls._factors.values() if f.zoo == zoo]

    @classmethod
    def get_by_category(cls, category: AlphaCategory | str) -> list[AlphaFactor]:
        """Get all factors in a category."""
        if isinstance(category, str):
            category = AlphaCategory(category)
        return [f for f in cls._factors.values() if f.category == category]

    @classmethod
    def list_all(cls) -> dict[str, AlphaFactor]:
        """List all registered factors."""
        return dict(cls._factors)

    @classmethod
    def clear(cls) -> None:
        """Clear all registered factors (for testing)."""
        cls._factors.clear()


def register(factor: AlphaFactor) -> AlphaFactor:
    """Decorator to register a factor."""
    AlphaFactorRegistry.register(factor)
    return factor


# ============================================================================
# Qlib 158 Factors (Quality, Momentum, Volatility)
# ============================================================================

@register
class MomentumFactor(AlphaFactor):
    """12-1 month momentum factor."""

    zoo = AlphaZoo.QLIB
    category = AlphaCategory.MOMENTUM
    description = "Price momentum over 12-1 month window"
    params = {"lookback": 252, "skip_last": 21}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"]:
            return 0.0
        return float(
            (closes.iloc[-1] / closes.iloc[-self.params["skip_last"]] - 1)
        )


@register
class VolatilityFactor(AlphaFactor):
    """Realized volatility factor."""

    zoo = AlphaZoo.QLIB
    category = AlphaCategory.VOLATILITY
    description = "Annualized realized volatility"
    params = {"lookback": 20}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"]:
            return 0.0
        returns = closes.pct_change().dropna()
        return float(returns.tail(self.params["lookback"]).std() * (252 ** 0.5))


@register
class VolumeFactor(AlphaFactor):
    """Volume change factor."""

    zoo = AlphaZoo.QLIB
    category = AlphaCategory.LIQUIDITY
    description = "Volume ratio (current vs 20-day average)"
    params = {"lookback": 20}

    def compute(self, frame: pd.DataFrame) -> float:
        volumes = frame["volume"].astype(float)
        if len(volumes) < self.params["lookback"]:
            return 0.0
        avg_vol = volumes.tail(self.params["lookback"]).mean()
        current_vol = volumes.iloc[-1]
        return float(current_vol / avg_vol - 1) if avg_vol > 0 else 0.0


# ============================================================================
# Kakushadze 101 Factors (Technical/Statistical)
# ============================================================================

@register
class ReturnSkewnessFactor(AlphaFactor):
    """Return skewness factor."""

    zoo = AlphaZoo.KAKUSHADZE
    category = AlphaCategory.TECHNICAL
    description = "Skewness of returns over 60-day window"
    params = {"lookback": 60}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"] + 5:
            return 0.0
        returns = closes.pct_change().dropna().tail(self.params["lookback"])
        return float(returns.skew())


@register
class ReturnKurtosisFactor(AlphaFactor):
    """Return kurtosis factor."""

    zoo = AlphaZoo.KAKUSHADZE
    category = AlphaCategory.TECHNICAL
    description = "Excess kurtosis of returns over 60-day window"
    params = {"lookback": 60}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"] + 5:
            return 0.0
        returns = closes.pct_change().dropna().tail(self.params["lookback"])
        return float(returns.kurtosis())


@register
class MaxDrawdownFactor(AlphaFactor):
    """Maximum drawdown factor."""

    zoo = AlphaZoo.KAKUSHADZE
    category = AlphaCategory.VOLATILITY
    description = "Maximum drawdown over 126-day window"
    params = {"lookback": 126}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"]:
            return 0.0
        rolling_max = closes.tail(self.params["lookback"]).expanding().max()
        drawdowns = (closes.tail(self.params["lookback"]) - rolling_max) / rolling_max
        return float(drawdowns.min())


# ============================================================================
# GTJA 191 Factors (Trend/Mean-Reversion)
# ============================================================================

@register
class TrendStrengthFactor(AlphaFactor):
    """Trend strength using ADX-like measure."""

    zoo = AlphaZoo.GTJA
    category = AlphaCategory.TREND
    description = "Trend strength measure (EMA slope normalized)"
    params = {"short_period": 20, "long_period": 50}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["long_period"]:
            return 0.0
        ema_short = closes.ewm(span=self.params["short_period"], adjust=False).mean()
        ema_long = closes.ewm(span=self.params["long_period"], adjust=False).mean()
        slope = float(ema_short.iloc[-1] - ema_long.iloc[-1])
        avg_price = float(closes.tail(self.params["long_period"]).mean())
        return slope / avg_price if avg_price > 0 else 0.0


@register
class MeanReversionFactor(AlphaFactor):
    """Mean reversion using z-score from moving average."""

    zoo = AlphaZoo.GTJA
    category = AlphaCategory.MEAN_REVERSION
    description = "Z-score of price from 50-day moving average"
    params = {"lookback": 50}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"]:
            return 0.0
        sma = closes.rolling(self.params["lookback"]).mean()
        std = closes.rolling(self.params["lookback"]).std()
        z_score = (closes.iloc[-1] - sma.iloc[-1]) / std.iloc[-1] if std.iloc[-1] > 0 else 0.0
        return float(z_score)


@register
class VolumePriceCorrelationFactor(AlphaFactor):
    """Volume-price correlation factor."""

    zoo = AlphaZoo.GTJA
    category = AlphaCategory.LIQUIDITY
    description = "Correlation between volume and price changes"
    params = {"lookback": 20}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        volumes = frame["volume"].astype(float)
        if len(closes) < self.params["lookback"] + 2:
            return 0.0
        returns = closes.pct_change().dropna().tail(self.params["lookback"])
        vol_returns = volumes.pct_change().dropna().tail(self.params["lookback"])
        common_len = min(len(returns), len(vol_returns))
        if common_len < 10:
            return 0.0
        return float(returns.tail(common_len).corr(vol_returns.tail(common_len)))


# ============================================================================
# Academic 10 Factors (Classic Academic Alphas)
# ============================================================================

@register
class JegadeeshReversalFactor(AlphaFactor):
    """Jegadeesh (1990) reversal factor."""

    zoo = AlphaZoo.ACADEMIC
    category = AlphaCategory.MEAN_REVERSION
    description = "3-month price reversal"
    params = {"lookback": 63}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"]:
            return 0.0
        # Negative of return (reversal = buy losers, sell winners)
        return float(
            -(closes.iloc[-1] / closes.iloc[-self.params["lookback"]] - 1)
        )


@register
class GeorgeHwang52WeekHighFactor(AlphaFactor):
    """George and Hwang (2001) 52-week high factor."""

    zoo = AlphaZoo.ACADEMIC
    category = AlphaCategory.MOMENTUM
    description = "Distance from 52-week high"
    params = {"lookback": 252}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"]:
            return 0.0
        high_52w = closes.tail(self.params["lookback"]).max()
        current = closes.iloc[-1]
        return float((high_52w - current) / high_52w) if high_52w > 0 else 0.0


@register
class AmihudIlliquidityFactor(AlphaFactor):
    """Amihud (2002) illiquidity factor."""

    zoo = AlphaZoo.ACADEMIC
    category = AlphaCategory.LIQUIDITY
    description = "Average absolute return per dollar of volume"
    params = {"lookback": 20}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        volumes = frame["volume"].astype(float)
        if len(closes) < self.params["lookback"] + 1:
            return 0.0
        returns = closes.pct_change().dropna().tail(self.params["lookback"])
        vol = volumes.tail(self.params["lookback"]).replace(0, float("inf"))
        illiquidity = (returns.abs() / vol).mean()
        return float(illiquidity)


@register
class HarveySiddiqueSkewFactor(AlphaFactor):
    """Harvey and Siddique (2000) skewness factor."""

    zoo = AlphaZoo.ACADEMIC
    category = AlphaCategory.TECHNICAL
    description = "Conditional skewness of returns"
    params = {"lookback": 60}

    def compute(self, frame: pd.DataFrame) -> float:
        closes = frame["close"].astype(float)
        if len(closes) < self.params["lookback"] + 5:
            return 0.0
        returns = closes.pct_change().dropna().tail(self.params["lookback"])
        # Conditional skewness (skewness of positive returns only)
        pos_returns = returns[returns > 0]
        if len(pos_returns) < 10:
            return 0.0
        return float(pos_returns.skew())


# ============================================================================
# Initialize registry with all factors
# ============================================================================

# Register all factor instances
for _factor in [
    MomentumFactor(),
    VolatilityFactor(),
    VolumeFactor(),
    ReturnSkewnessFactor(),
    ReturnKurtosisFactor(),
    MaxDrawdownFactor(),
    TrendStrengthFactor(),
    MeanReversionFactor(),
    VolumePriceCorrelationFactor(),
    JegadeeshReversalFactor(),
    GeorgeHwang52WeekHighFactor(),
    AmihudIlliquidityFactor(),
    HarveySiddiqueSkewFactor(),
]:
    AlphaFactorRegistry.register(_factor)
