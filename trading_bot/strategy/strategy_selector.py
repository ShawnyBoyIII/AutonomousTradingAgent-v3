"""Dynamic strategy selection based on market conditions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

from trading_bot.models.signal import TradeSignal
from trading_bot.strategy.market_regime import (
    MarketRegime,
    RegimeMetrics,
    detect_market_regime,
    get_recommended_strategy,
    should_trade_regime,
)
from trading_bot.strategy.signal_confluence import (
    SignalScore,
    calculate_signal_confluence,
)
from trading_bot.strategy.setup_rules import identify_intraday_setup
from trading_bot.strategy.mean_reversion import identify_mean_reversion_setup

# V3 confidence label → numeric weight (0.0-1.0) for TradeSignal.
CONFIDENCE_WEIGHTS: dict[str, float] = {
    "none": 0.0,
    "low": 0.3,
    "medium": 0.55,
    "high": 0.75,
    "very_high": 0.9,
}


@dataclass
class StrategySelection:
    """Result of strategy selection process."""

    should_trade: bool
    strategy_type: str
    setup_name: str | None
    signal_score: SignalScore | None
    regime: MarketRegime | None
    reason: str

    # Trade parameters
    entry_price: float | None = None
    stop_loss: float | None = None
    profit_target: float | None = None
    position_size_multiplier: float = 0.0


class StrategySelector:
    """Selects optimal strategy based on market regime and signal quality."""

    def __init__(self, risk_tolerance: str = "medium") -> None:
        self.risk_tolerance = risk_tolerance
        self.min_confidence = "medium"  # Minimum confidence to trade
        self.atr_stop_multiplier = 1.5  # Min ATR distance for stops

    def select_strategy(
        self,
        symbol: str,
        daily_frame: "pd.DataFrame",
        intraday_frame: "pd.DataFrame",
    ) -> StrategySelection:
        """Select best strategy and determine trade parameters.

        This is the main entry point for dynamic strategy selection.
        It analyzes market conditions and selects the optimal approach.
        """
        # Step 1: Detect market regime
        regime, regime_metrics = detect_market_regime(daily_frame)

        # Step 2: Check if we should trade at all
        if not should_trade_regime(regime, self.risk_tolerance):
            return StrategySelection(
                should_trade=False,
                strategy_type="none",
                setup_name=None,
                signal_score=None,
                regime=regime,
                reason=f"Unfavorable regime: {regime.value}",
            )

        # Step 3: Get recommended strategy type
        recommended_strategy = get_recommended_strategy(regime)

        # Step 4: Try to find signals for appropriate strategies
        selection = self._evaluate_strategies(
            symbol,
            daily_frame,
            intraday_frame,
            regime,
            regime_metrics,
            recommended_strategy,
        )

        return selection

    def _evaluate_strategies(
        self,
        symbol: str,
        daily_frame: "pd.DataFrame",
        intraday_frame: "pd.DataFrame",
        regime: MarketRegime,
        regime_metrics: RegimeMetrics,
        recommended_strategy: str,
    ) -> StrategySelection:
        """Evaluate available strategies and pick the best one."""

        candidates = []

        # Try trend-following strategies if appropriate
        if recommended_strategy == "trend_following":
            trend_setup = identify_intraday_setup(intraday_frame)
            if trend_setup:
                score = calculate_signal_confluence(
                    symbol=symbol,
                    daily_frame=daily_frame,
                    intraday_frame=intraday_frame,
                    regime=regime,
                    regime_metrics=regime_metrics,
                    setup_type="breakout" if "breakout" in trend_setup else "momentum",
                )
                candidates.append(("trend_following", trend_setup, score))

        # Try mean reversion strategies if appropriate
        if recommended_strategy == "mean_reversion":
            reversion_setup = identify_mean_reversion_setup(intraday_frame)
            if reversion_setup:
                score = calculate_signal_confluence(
                    symbol=symbol,
                    daily_frame=daily_frame,
                    intraday_frame=intraday_frame,
                    regime=regime,
                    regime_metrics=regime_metrics,
                    setup_type=reversion_setup.replace(" ", "_"),
                )
                candidates.append(("mean_reversion", reversion_setup, score))

        # If no recommended strategy signals, try the opposite
        if not candidates:
            if recommended_strategy == "trend_following":
                # Try mean reversion as fallback
                reversion_setup = identify_mean_reversion_setup(intraday_frame)
                if reversion_setup:
                    score = calculate_signal_confluence(
                        symbol=symbol,
                        daily_frame=daily_frame,
                        intraday_frame=intraday_frame,
                        regime=regime,
                        regime_metrics=regime_metrics,
                        setup_type=reversion_setup.replace(" ", "_"),
                    )
                    # Penalize for regime mismatch
                    score.regime_alignment *= 0.5
                    score.total_score = (
                        score.technical_score
                        + score.volume_score
                        + score.trend_score
                        + score.momentum_score
                        + score.regime_alignment
                    )
                    score.confidence = self._recalculate_confidence(score.total_score)
                    candidates.append(("mean_reversion", reversion_setup, score))

            elif recommended_strategy == "mean_reversion":
                # Try trend following as fallback
                trend_setup = identify_intraday_setup(intraday_frame)
                if trend_setup:
                    score = calculate_signal_confluence(
                        symbol=symbol,
                        daily_frame=daily_frame,
                        intraday_frame=intraday_frame,
                        regime=regime,
                        regime_metrics=regime_metrics,
                        setup_type="breakout" if "breakout" in trend_setup else "momentum",
                    )
                    # Penalize for regime mismatch
                    score.regime_alignment *= 0.5
                    score.total_score = (
                        score.technical_score
                        + score.volume_score
                        + score.trend_score
                        + score.momentum_score
                        + score.regime_alignment
                    )
                    score.confidence = self._recalculate_confidence(score.total_score)
                    candidates.append(("trend_following", trend_setup, score))

            elif recommended_strategy == "none":
                # High volatility or strong downtrend - try both but with penalties
                # First try trend following
                trend_setup = identify_intraday_setup(intraday_frame)
                if trend_setup:
                    score = calculate_signal_confluence(
                        symbol=symbol,
                        daily_frame=daily_frame,
                        intraday_frame=intraday_frame,
                        regime=regime,
                        regime_metrics=regime_metrics,
                        setup_type="breakout" if "breakout" in trend_setup else "momentum",
                    )
                    score.regime_alignment *= 0.3  # Heavy penalty for adverse regime
                    score.total_score = (
                        score.technical_score
                        + score.volume_score
                        + score.trend_score
                        + score.momentum_score
                        + score.regime_alignment
                    )
                    score.confidence = self._recalculate_confidence(score.total_score)
                    candidates.append(("trend_following", trend_setup, score))
                
                # Also try mean reversion
                reversion_setup = identify_mean_reversion_setup(intraday_frame)
                if reversion_setup:
                    score = calculate_signal_confluence(
                        symbol=symbol,
                        daily_frame=daily_frame,
                        intraday_frame=intraday_frame,
                        regime=regime,
                        regime_metrics=regime_metrics,
                        setup_type=reversion_setup.replace(" ", "_"),
                    )
                    score.regime_alignment *= 0.3
                    score.total_score = (
                        score.technical_score
                        + score.volume_score
                        + score.trend_score
                        + score.momentum_score
                        + score.regime_alignment
                    )
                    score.confidence = self._recalculate_confidence(score.total_score)
                    candidates.append(("mean_reversion", reversion_setup, score))

        # Select best candidate
        if not candidates:
            return StrategySelection(
                should_trade=False,
                strategy_type="none",
                setup_name=None,
                signal_score=None,
                regime=regime,
                reason="No valid setups found",
            )

        # Pick highest scoring candidate
        best = max(candidates, key=lambda x: x[2].total_score)
        strategy_type, setup_name, score = best

        # Check minimum confidence
        if not self._meets_confidence_threshold(score.confidence):
            return StrategySelection(
                should_trade=False,
                strategy_type=strategy_type,
                setup_name=setup_name,
                signal_score=score,
                regime=regime,
                reason=f"Confidence too low: {score.confidence} (need {self.min_confidence})",
            )

        # Calculate trade parameters
        entry, stop, target = self._calculate_trade_parameters(
            intraday_frame, setup_name
        )

        return StrategySelection(
            should_trade=True,
            strategy_type=strategy_type,
            setup_name=setup_name,
            signal_score=score,
            regime=regime,
            reason=f"High confluence {strategy_type} signal: {setup_name}",
            entry_price=entry,
            stop_loss=stop,
            profit_target=target,
            position_size_multiplier=score.recommended_position_size_pct,
        )

    def _meets_confidence_threshold(self, confidence: str) -> bool:
        """Check if confidence meets minimum threshold."""
        levels = ["none", "low", "medium", "high", "very_high"]
        try:
            return levels.index(confidence) >= levels.index(self.min_confidence)
        except ValueError:
            return False

    def _recalculate_confidence(self, total_score: float) -> str:
        """Recalculate confidence after score adjustment."""
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

    def _calculate_trade_parameters(
        self,
        intraday_frame: "pd.DataFrame",
        setup_name: str,
    ) -> tuple[float | None, float | None, float | None]:
        """Calculate entry, stop, and target prices."""
        if len(intraday_frame) < 1:
            return None, None, None

        latest = intraday_frame.iloc[-1]
        entry = float(latest["close"])

        # Calculate stop based on setup type
        if "breakout" in setup_name.lower():
            # Stop below recent support
            lows = intraday_frame["low"].tail(5).tolist()
            low_stop = min(lows) if lows else entry * 0.98
        elif "momentum" in setup_name.lower():
            # Stop at previous bar low
            low_stop = float(intraday_frame.iloc[-2]["low"]) if len(intraday_frame) > 1 else entry * 0.98
        elif "oversold" in setup_name.lower() or "reversion" in setup_name.lower():
            # Tighter stop for mean reversion
            low_stop = entry * 0.99
        else:
            # Default: 1% stop
            low_stop = entry * 0.99

        # ATR floor: ensure stop is at least atr × multiplier below entry
        # Prevents 0.2–0.6% noise stops on tight consolidations
        atr_value = None
        if hasattr(latest, "get"):
            try:
                raw_atr = latest.get("atr_14")
                if raw_atr is not None:
                    atr_value = float(raw_atr)
                    if not (atr_value == atr_value):  # NaN check
                        atr_value = None
            except (TypeError, ValueError):
                atr_value = None
        if atr_value is not None and atr_value > 0:
            atr_floor = entry - (atr_value * self.atr_stop_multiplier)
            stop = min(low_stop, atr_floor)
        else:
            stop = low_stop

        # Guard against negative or zero stop (can happen with high ATR / low price)
        if stop <= 0:
            stop = entry * 0.99

        # 2:1 risk-reward target
        risk = entry - stop
        target = entry + (risk * 2)

        return entry, stop, target


def select_optimal_strategy(
    symbol: str,
    daily_frame: "pd.DataFrame",
    intraday_frame: "pd.DataFrame",
    risk_tolerance: str = "medium",
) -> StrategySelection:
    """Convenience function for strategy selection.

    Args:
        symbol: Ticker symbol
        daily_frame: Daily OHLCV data with indicators
        intraday_frame: Intraday OHLCV data with indicators
        risk_tolerance: low, medium, or high

    Returns:
        StrategySelection with trade decision and parameters
    """
    selector = StrategySelector(risk_tolerance=risk_tolerance)
    return selector.select_strategy(symbol, daily_frame, intraday_frame)


def selection_to_signal(
    symbol: str,
    selection: StrategySelection,
    intraday_frame: "pd.DataFrame",
) -> TradeSignal | None:
    """Adapt a V3 StrategySelection to the TradeSignal shape.

    Guards against None entry/stop/target and enforces the TradeSignal
    BUY geometry (stop < entry < target) by falling back to a tight
    default stop when the selector returns None values.

    Shared between the orchestrator and the backtest runner so both
   .paths produce identical TradeSignal structures.
    """
    entry = selection.entry_price
    if entry is None:
        if intraday_frame.empty:
            return None
        entry = float(intraday_frame.iloc[-1]["close"])
        if entry <= 0:
            return None
    entry = round(entry, 4)

    stop = selection.stop_loss
    if stop is None or stop >= entry or stop <= 0:
        stop = round(entry * 0.99, 4)
    stop = round(stop, 4)

    target = selection.profit_target
    if target is None or target <= entry:
        target = round(entry + (entry - stop) * 2.0, 4)
    target = round(target, 4)

    risk = entry - stop
    if risk <= 0:
        return None

    confidence_label = (
        selection.signal_score.confidence if selection.signal_score else "none"
    )
    confidence = CONFIDENCE_WEIGHTS.get(confidence_label, 0.0)

    timestamp = None
    if not intraday_frame.empty and "timestamp" in intraday_frame.columns:
        candidate = intraday_frame.iloc[-1].get("timestamp")
        if isinstance(candidate, datetime):
            timestamp = candidate
    if timestamp is None and not intraday_frame.empty:
        timestamp = datetime.now()

    if timestamp is None:
        return None

    reasons = list(selection.signal_score.supporting_signals or [])
    if not reasons:
        reasons = [selection.reason]

    try:
        return TradeSignal(
            ticker=symbol,
            timeframe="intraday",
            action="BUY",
            entry_price=entry,
            stop_loss=stop,
            profit_target=target,
            risk_reward_ratio=round((target - entry) / risk, 6),
            confidence=confidence,
            reasons=reasons,
            strategy_tag=f"v3-{selection.strategy_type}",
            timestamp=timestamp,
        )
    except ValueError:
        return None
