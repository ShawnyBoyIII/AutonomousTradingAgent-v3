"""Signal confluence scoring for trade quality assessment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from trading_bot.strategy.market_regime import MarketRegime, RegimeMetrics


@dataclass
class SignalScore:
    """Comprehensive signal score with components."""

    total_score: float = 0.0  # 0.0 to 10.0
    confidence: str = "none"  # none, low, medium, high, very_high

    # Component scores (each 0.0 to 2.0)
    technical_score: float = 0.0
    volume_score: float = 0.0
    trend_score: float = 0.0
    momentum_score: float = 0.0
    regime_alignment: float = 0.0

    # Factors
    supporting_signals: list[str] = field(default_factory=list)
    opposing_signals: list[str] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)

    # Metadata
    setup_type: str = ""
    recommended_position_size_pct: float = 0.0  # 0.0 to 1.0


def calculate_signal_confluence(
    symbol: str,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    regime: MarketRegime,
    regime_metrics: RegimeMetrics,
    setup_type: str,
) -> SignalScore:
    """Calculate comprehensive signal confluence score.

    Combines multiple technical factors to produce a quality score
    that determines trade confidence and position size.
    """
    score = SignalScore(setup_type=setup_type)

    # 1. Technical Setup Score (0-2)
    score.technical_score = _score_technical_setup(
        daily_frame, intraday_frame, setup_type
    )

    # 2. Volume Confirmation Score (0-2)
    score.volume_score = _score_volume_confirmation(intraday_frame)

    # 3. Trend Alignment Score (0-2)
    score.trend_score = _score_trend_alignment(
        daily_frame, regime, regime_metrics
    )

    # 4. Momentum Score (0-2)
    score.momentum_score = _score_momentum(intraday_frame, setup_type)

    # 5. Regime Alignment Score (0-2)
    score.regime_alignment = _score_regime_alignment(regime, setup_type)

    # Calculate total
    score.total_score = (
        score.technical_score
        + score.volume_score
        + score.trend_score
        + score.momentum_score
        + score.regime_alignment
    )

    # Determine confidence level
    score.confidence = _score_to_confidence(score.total_score)

    # Calculate recommended position size
    score.recommended_position_size_pct = _calculate_position_size_multiplier(
        score, regime
    )

    # Identify signals and risks
    score.supporting_signals = _identify_supporting_signals(
        score, daily_frame, intraday_frame
    )
    score.opposing_signals = _identify_opposing_signals(
        score, daily_frame, intraday_frame
    )
    score.risk_factors = _identify_risk_factors(score, regime, regime_metrics)

    return score


def _score_technical_setup(
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    setup_type: str,
) -> float:
    """Score technical setup quality (0-2)."""
    score = 1.0  # Base score

    # Check for clean support/resistance levels
    if len(intraday_frame) >= 20:
        highs = intraday_frame["high"].tail(20)
        lows = intraday_frame["low"].tail(20)

        # Reward clear ranges (consistent highs/lows)
        high_consistency = 1 - (highs.std() / highs.mean())
        low_consistency = 1 - (lows.std() / lows.mean())

        if high_consistency > 0.95 and low_consistency > 0.95:
            score += 0.3

    # Check for confluence with daily levels
    if len(daily_frame) >= 5 and len(intraday_frame) >= 1:
        latest_intraday = float(intraday_frame.iloc[-1]["close"])
        daily_high = float(daily_frame["high"].tail(5).max())
        daily_low = float(daily_frame["low"].tail(5).min())

        # Trading near daily extremes (potential breakout/bounce)
        daily_range = daily_high - daily_low
        if daily_range > 0:
            position_in_range = (latest_intraday - daily_low) / daily_range

            if setup_type == "breakout" and position_in_range > 0.7:
                score += 0.4
            elif setup_type == "mean_reversion" and position_in_range < 0.3:
                score += 0.4

    # Check moving average alignment
    if "ema_20" in daily_frame.columns and "sma_50" in daily_frame.columns:
        ema20_val = daily_frame.iloc[-1]["ema_20"]
        sma50_val = daily_frame.iloc[-1]["sma_50"]
        if ema20_val is not None and sma50_val is not None:
            ema20 = float(ema20_val)
            sma50 = float(sma50_val)
            if setup_type == "breakout" and ema20 > sma50:
                score += 0.3
            elif setup_type == "mean_reversion" and ema20 < sma50:
                score += 0.3

    return min(2.0, score)


def _score_volume_confirmation(intraday_frame: "pd.DataFrame") -> float:
    """Score volume confirmation (0-2)."""
    if len(intraday_frame) < 5 or "volume" not in intraday_frame.columns:
        return 1.0

    latest_volume = float(intraday_frame.iloc[-1]["volume"])
    avg_volume = float(intraday_frame["volume"].tail(5).mean())

    if avg_volume == 0:
        return 1.0

    volume_ratio = latest_volume / avg_volume

    # Score based on volume ratio
    if volume_ratio >= 2.0:
        return 2.0
    elif volume_ratio >= 1.5:
        return 1.7
    elif volume_ratio >= 1.0:
        return 1.4
    elif volume_ratio >= 0.8:
        return 1.0
    else:
        return 0.6


def _score_trend_alignment(
    daily_frame: "pd.DataFrame",
    regime: MarketRegime,
    regime_metrics: RegimeMetrics,
) -> float:
    """Score trend alignment (0-2)."""
    score = 1.0

    # ADX-based trend strength
    adx = regime_metrics.adx
    if adx > 40:
        score += 0.5
    elif adx > 25:
        score += 0.3
    elif adx < 15:
        score -= 0.3

    # Regime appropriateness
    if regime in [MarketRegime.STRONG_UPTREND, MarketRegime.WEAK_UPTREND]:
        score += 0.3
    elif regime in [MarketRegime.RANGE_BOUND]:
        score += 0.1
    elif regime in [MarketRegime.STRONG_DOWNTREND]:
        score -= 0.5

    # Price vs moving averages
    if regime_metrics.price_vs_sma50 > 2:
        score += 0.2
    elif regime_metrics.price_vs_sma50 < -2:
        score -= 0.2

    return max(0.0, min(2.0, score))


def _score_momentum(intraday_frame: "pd.DataFrame", setup_type: str) -> float:
    """Score momentum alignment (0-2)."""
    if len(intraday_frame) < 3:
        return 1.0

    # Calculate short-term momentum
    closes = intraday_frame["close"].astype(float).tolist()
    momentum_3 = (closes[-1] - closes[-3]) / closes[-3] if closes[-3] != 0 else 0

    # RSI if available
    rsi_score = 1.0
    if "rsi_14" in intraday_frame.columns:
        rsi_raw = intraday_frame.iloc[-1]["rsi_14"]
        try:
            rsi = float(rsi_raw)
        except (TypeError, ValueError):
            rsi = None
        if rsi is not None:
            if setup_type == "breakout":
                # For breakouts, moderate RSI is good (not overbought)
                if 50 <= rsi <= 70:
                    rsi_score = 1.3
                elif rsi > 70:
                    rsi_score = 0.7
            elif setup_type in ("mean_reversion", "mean_reversion_bounce", "oversold_bounce", "vwap_reversion", "range_reversal"):
                # For mean reversion, oversold is good
                if rsi < 35:
                    rsi_score = 1.5
                elif rsi > 60:
                    rsi_score = 0.6

    # Combine momentum and RSI
    if setup_type == "breakout":
        if momentum_3 > 0.01:  # Positive momentum
            return min(2.0, 1.2 + (rsi_score - 1.0))
        else:
            return max(0.0, 0.8 + (rsi_score - 1.0))
    else:  # mean_reversion
        if momentum_3 > -0.005:  # Not falling too fast
            return min(2.0, 1.1 + (rsi_score - 1.0))
        else:
            return max(0.0, 0.7 + (rsi_score - 1.0))


def _score_regime_alignment(regime: MarketRegime, setup_type: str) -> float:
    """Score regime-strategy alignment (0-2)."""
    # Perfect alignment scores 2.0
    # Poor alignment scores 0.0-0.5

    regime_strategy_map = {
        MarketRegime.STRONG_UPTREND: "trend_following",
        MarketRegime.WEAK_UPTREND: "trend_following",
        MarketRegime.RANGE_BOUND: "mean_reversion",
        MarketRegime.WEAK_DOWNTREND: "mean_reversion",
        MarketRegime.STRONG_DOWNTREND: "none",
        MarketRegime.HIGH_VOLATILITY: "none",
    }

    recommended = regime_strategy_map.get(regime, "none")

    if setup_type == "breakout":
        if recommended == "trend_following":
            return 2.0
        elif recommended == "mean_reversion":
            return 0.5
        else:
            return 0.0
    elif setup_type in ["oversold bounce", "vwap reversion", "range reversal"]:
        if recommended == "mean_reversion":
            return 2.0
        elif recommended == "trend_following":
            return 0.7
        else:
            return 0.0
    else:
        return 1.0


def _score_to_confidence(total_score: float) -> str:
    """Convert total score to confidence level."""
    if total_score >= 8.5:
        return "very_high"
    elif total_score >= 7.0:
        return "high"
    elif total_score >= 5.5:
        return "medium"
    elif total_score >= 4.0:
        return "low"
    else:
        return "none"


def _calculate_position_size_multiplier(
    score: SignalScore, regime: MarketRegime
) -> float:
    """Calculate recommended position size as percentage of max."""
    base_multiplier = score.total_score / 10.0  # 0.0 to 1.0

    # Reduce size in unfavorable regimes
    regime_multipliers = {
        MarketRegime.STRONG_UPTREND: 1.0,
        MarketRegime.WEAK_UPTREND: 0.9,
        MarketRegime.RANGE_BOUND: 0.8,
        MarketRegime.WEAK_DOWNTREND: 0.6,
        MarketRegime.STRONG_DOWNTREND: 0.0,
        MarketRegime.HIGH_VOLATILITY: 0.5,
    }

    regime_mult = regime_multipliers.get(regime, 0.7)

    # Confidence adjustment
    confidence_multipliers = {
        "very_high": 1.0,
        "high": 0.9,
        "medium": 0.7,
        "low": 0.5,
        "none": 0.0,
    }

    conf_mult = confidence_multipliers.get(score.confidence, 0.0)

    return base_multiplier * regime_mult * conf_mult


def _identify_supporting_signals(
    score: SignalScore,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
) -> list[str]:
    """Identify supporting technical signals."""
    signals = []

    if score.volume_score >= 1.5:
        signals.append("Strong volume confirmation")

    if score.trend_score >= 1.5:
        signals.append("Trend alignment")

    if score.momentum_score >= 1.3:
        signals.append("Positive momentum")

    if score.technical_score >= 1.5:
        signals.append("Clean technical setup")

    # Check for specific patterns
    if len(intraday_frame) >= 3:
        closes = intraday_frame["close"].tail(3).tolist()
        if closes[0] < closes[1] < closes[2]:
            signals.append("Three-bar upward momentum")

    return signals


def _identify_opposing_signals(
    score: SignalScore,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
) -> list[str]:
    """Identify opposing/red flags."""
    signals = []

    if score.volume_score < 0.8:
        signals.append("Low volume")

    if score.trend_score < 0.7:
        signals.append("Trend disagreement")

    if score.momentum_score < 0.7:
        signals.append("Negative momentum")

    # Check for overextension
    if "rsi_14" in intraday_frame.columns:
        try:
            rsi = float(intraday_frame.iloc[-1]["rsi_14"])
        except (TypeError, ValueError):
            rsi = None
        if rsi is not None:
            if rsi > 75:
                signals.append("Overbought (RSI > 75)")
            elif rsi < 25:
                signals.append("Oversold (RSI < 25)")

    return signals


def _identify_risk_factors(
    score: SignalScore,
    regime: MarketRegime,
    regime_metrics: RegimeMetrics,
) -> list[str]:
    """Identify risk factors for the trade."""
    risks = []

    if regime == MarketRegime.HIGH_VOLATILITY:
        risks.append("High volatility regime")

    if regime == MarketRegime.STRONG_DOWNTREND:
        risks.append("Strong downtrend - countertrend trade")

    if regime_metrics.volatility_percentile > 0.9:
        risks.append("Unusually high volatility")

    if score.total_score < 6.0:
        risks.append("Low confluence score")

    return risks
