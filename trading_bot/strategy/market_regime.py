"""Market regime detection for dynamic strategy selection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


class MarketRegime(Enum):
    """Market regime classification."""

    STRONG_UPTREND = "strong_uptrend"
    WEAK_UPTREND = "weak_uptrend"
    RANGE_BOUND = "range_bound"
    WEAK_DOWNTREND = "weak_downtrend"
    STRONG_DOWNTREND = "strong_downtrend"
    HIGH_VOLATILITY = "high_volatility"


@dataclass
class RegimeMetrics:
    """Metrics used for regime classification."""

    trend_strength: float = 0.0  # -1.0 to 1.0
    volatility_percentile: float = 0.0  # 0.0 to 1.0
    adx: float = 0.0  # 0 to 100
    price_vs_ema20: float = 0.0  # Percentage
    price_vs_sma50: float = 0.0  # Percentage
    bb_squeeze: bool = False
    momentum: float = 0.0  # -1.0 to 1.0


def detect_market_regime(daily_frame: "pd.DataFrame") -> tuple[MarketRegime, RegimeMetrics]:
    """Detect current market regime from daily price data.

    Uses ADX for trend strength, Bollinger Bands for volatility,
    and moving average alignment for direction.

    Returns:
        Tuple of (MarketRegime, RegimeMetrics)
    """
    required_columns = {"close", "high", "low", "ema_20", "sma_50", "atr_14"}
    if len(daily_frame) < 50 or not required_columns.issubset(daily_frame.columns):
        return MarketRegime.RANGE_BOUND, RegimeMetrics()

    latest = daily_frame.iloc[-1]
    close = float(latest["close"])
    ema20 = float(latest["ema_20"])
    sma50 = float(latest["sma_50"])

    # Calculate ADX (Average Directional Index) approximation
    adx = _calculate_adx(daily_frame)

    # Calculate Bollinger Band width for volatility assessment
    bb_width, bb_squeeze = _calculate_bb_metrics(daily_frame)

    # Calculate volatility percentile (vs last 20 days)
    volatility_pct = _calculate_volatility_percentile(daily_frame)

    # Price relative to moving averages
    price_vs_ema20 = (close - ema20) / ema20 * 100
    price_vs_sma50 = (close - sma50) / sma50 * 100

    # Momentum (rate of change)
    momentum = _calculate_momentum(daily_frame)

    metrics = RegimeMetrics(
        trend_strength=_normalize(adx, 0, 50),
        volatility_percentile=volatility_pct,
        adx=adx,
        price_vs_ema20=price_vs_ema20,
        price_vs_sma50=price_vs_sma50,
        bb_squeeze=bb_squeeze,
        momentum=momentum,
    )

    # Classify regime
    regime = _classify_regime(metrics)

    return regime, metrics


def _calculate_adx(frame: "pd.DataFrame", period: int = 14) -> float:
    """Calculate ADX (Average Directional Index) approximation."""
    if len(frame) < period + 1:
        return 25.0  # Neutral default

    highs = frame["high"].astype(float).tolist()
    lows = frame["low"].astype(float).tolist()
    closes = frame["close"].astype(float).tolist()

    # Calculate +DM and -DM
    plus_dm = []
    minus_dm = []
    tr_values = []

    for i in range(1, len(frame)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0)

        # True Range
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_values.append(tr)

    # Calculate smoothed values
    if len(tr_values) < period:
        return 25.0

    atr = sum(tr_values[-period:]) / period
    plus_di = 100 * sum(plus_dm[-period:]) / (period * atr) if atr > 0 else 0
    minus_di = 100 * sum(minus_dm[-period:]) / (period * atr) if atr > 0 else 0

    # DX and ADX
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0

    return dx


def _calculate_bb_metrics(frame: "pd.DataFrame", period: int = 20) -> tuple[float, bool]:
    """Calculate Bollinger Band width and squeeze detection."""
    if len(frame) < period or "bb_upper" not in frame.columns:
        return 0.0, False

    latest = frame.iloc[-1]
    if "bb_middle" not in latest or "bb_width" not in latest:
        return 0.0, False

    bb_width = float(latest.get("bb_width", 0))

    # Squeeze = width in bottom 10% of recent history
    recent_widths = frame["bb_width"].dropna().tail(50).tolist()
    if len(recent_widths) >= 20:
        sorted_widths = sorted(recent_widths)
        threshold_idx = int(len(sorted_widths) * 0.1)
        squeeze_threshold = sorted_widths[threshold_idx] if threshold_idx < len(sorted_widths) else 0
        is_squeeze = bb_width <= squeeze_threshold
    else:
        is_squeeze = bb_width < 5.0  # Default threshold

    return bb_width, is_squeeze


def _calculate_volatility_percentile(frame: "pd.DataFrame", lookback: int = 20) -> float:
    """Calculate current volatility percentile vs recent history."""
    if len(frame) < lookback + 1 or "atr_14" not in frame.columns:
        return 0.5

    current_atr = float(frame.iloc[-1]["atr_14"])
    historical_atr = frame["atr_14"].dropna().tail(lookback).tolist()

    if not historical_atr:
        return 0.5

    count_below = sum(1 for atr in historical_atr if atr <= current_atr)
    return count_below / len(historical_atr)


def _calculate_momentum(frame: "pd.DataFrame", period: int = 10) -> float:
    """Calculate normalized momentum (-1 to 1)."""
    if len(frame) < period:
        return 0.0

    closes = frame["close"].astype(float).tolist()
    old_price = closes[-period]
    new_price = closes[-1]

    if old_price <= 0:
        return 0.0

    roc = (new_price - old_price) / old_price * 100
    # Normalize to -1 to 1 (assuming max reasonable ROC is 20%)
    return max(-1.0, min(1.0, roc / 20.0))


def _normalize(value: float, min_val: float, max_val: float) -> float:
    """Normalize value to -1 to 1 range."""
    if max_val == min_val:
        return 0.0
    normalized = 2 * (value - min_val) / (max_val - min_val) - 1
    return max(-1.0, min(1.0, normalized))


def _classify_regime(metrics: RegimeMetrics) -> MarketRegime:
    """Classify market regime based on metrics."""
    adx = metrics.adx
    price_vs_50 = metrics.price_vs_sma50
    volatility = metrics.volatility_percentile
    momentum = metrics.momentum

    # High volatility regime check:
    # - Volatility in top 20% (percentile > 0.8)
    # - OR extremely strong trend (ADX > 50) indicates volatile directional movement
    # Note: BB squeeze alone doesn't indicate high volatility - it indicates coiling
    # ADX > 50 is very rare (<5% of days) - only the strongest trends
    if volatility > 0.8 or adx > 50:
        return MarketRegime.HIGH_VOLATILITY

    # Trending regimes (ADX > 25 indicates trend)
    if adx > 25:
        if price_vs_50 > 5:  # Strong uptrend
            if momentum > 0.5:
                return MarketRegime.STRONG_UPTREND
            return MarketRegime.WEAK_UPTREND
        elif price_vs_50 < -5:  # Strong downtrend
            if momentum < -0.5:
                return MarketRegime.STRONG_DOWNTREND
            return MarketRegime.WEAK_DOWNTREND

    # Default to range-bound
    return MarketRegime.RANGE_BOUND


def get_recommended_strategy(regime: MarketRegime) -> str:
    """Get recommended strategy type for current regime."""
    recommendations = {
        MarketRegime.STRONG_UPTREND: "trend_following",
        MarketRegime.WEAK_UPTREND: "trend_following",
        MarketRegime.RANGE_BOUND: "mean_reversion",
        MarketRegime.WEAK_DOWNTREND: "mean_reversion",  # Short-term bounces
        MarketRegime.STRONG_DOWNTREND: "none",  # Avoid trading
        MarketRegime.HIGH_VOLATILITY: "none",  # Reduce size or avoid
    }
    return recommendations.get(regime, "range_bound")


def should_trade_regime(regime: MarketRegime, risk_tolerance: str = "medium") -> bool:
    """Determine if we should trade in current regime."""
    if risk_tolerance == "low":
        return regime in [MarketRegime.STRONG_UPTREND, MarketRegime.RANGE_BOUND]

    if risk_tolerance == "medium":
        return regime not in [MarketRegime.STRONG_DOWNTREND, MarketRegime.HIGH_VOLATILITY]

    # High risk tolerance - trade everything
    return True
