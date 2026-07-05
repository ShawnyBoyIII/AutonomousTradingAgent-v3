from __future__ import annotations

from typing import TYPE_CHECKING

from trading_bot.config.settings import RiskSettings
from trading_bot.models.risk import RiskDecision
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.position_sizer import (
    apply_fractional_kelly,
    calculate_atr_position_size,
    calculate_fixed_stop_position_size,
)

if TYPE_CHECKING:
    from trading_bot.strategy.counter_thesis import CounterThesisResult


def evaluate_signal(
    signal: TradeSignal,
    account_equity: float,
    open_tickers: set[str],
    portfolio_heat_pct: float,
    atr: float | None = None,
    risk_settings: RiskSettings | None = None,
    counter_thesis: "CounterThesisResult | None" = None,
    avg_correlation: float | None = None,
    max_avg_correlation: float | None = None,
) -> RiskDecision:
    """Evaluate a trade signal and return a risk decision.

    Args:
        signal: The trade signal to evaluate
        account_equity: Current account equity
        open_tickers: Set of currently open tickers (to check for duplicates)
        portfolio_heat_pct: Current portfolio heat (unrealized loss %)
        atr: 14-period ATR for volatility-adjusted sizing (optional)
        risk_settings: Risk configuration
        counter_thesis: Optional counter-thesis result; when its
            ``block_trade`` flag is set the signal is rejected, and its
            ``confidence_multiplier`` scales the position size down.

    Returns:
        RiskDecision with approval status and position size
    """
    settings = risk_settings or RiskSettings()

    # V2.5: Check portfolio heat limit first
    if portfolio_heat_pct >= settings.max_portfolio_heat_pct:
        return RiskDecision(
            approved=False,
            reason=f"portfolio heat limit exceeded ({portfolio_heat_pct:.2%} >= {settings.max_portfolio_heat_pct:.2%})",
            position_size=0,
            dollar_risk=0.0,
        )

    if signal.action != "BUY":
        return RiskDecision(
            approved=False,
            reason="unsupported signal action",
            position_size=0,
            dollar_risk=0.0,
        )

    if account_equity <= 0:
        return RiskDecision(
            approved=False,
            reason="invalid account equity",
            position_size=0,
            dollar_risk=0.0,
        )

    if signal.risk_reward_ratio < settings.min_reward_risk_ratio:
        return RiskDecision(
            approved=False,
            reason="reward/risk below minimum",
            position_size=0,
            dollar_risk=0.0,
        )

    if signal.ticker in open_tickers:
        return RiskDecision(
            approved=False,
            reason="duplicate open ticker",
            position_size=0,
            dollar_risk=0.0,
        )

    # V3: Counter-thesis veto. A blocked counter-thesis vetoes the trade
    # outright (a None result never vetoes so a data outage stays safe).
    if counter_thesis is not None and counter_thesis.block_trade:
        return RiskDecision(
            approved=False,
            reason=f"counter-thesis blocked: {'; '.join(counter_thesis.reasons)}",
            position_size=0,
            dollar_risk=0.0,
        )

    # V2.5: Use ATR-based sizing if enabled and ATR is available
    if settings.use_atr_sizing and atr is not None and atr > 0:
        position_size = calculate_atr_position_size(
            account_equity=account_equity,
            risk_pct=settings.max_risk_per_trade_pct,
            entry_price=signal.entry_price,
            atr=atr,
            atr_multiplier=settings.atr_multiplier,
            max_position_pct=settings.max_ticker_allocation_pct,
        )
        # Dollar risk is based on ATR stop distance
        effective_stop_distance = atr * settings.atr_multiplier
        dollar_risk = position_size * effective_stop_distance
    else:
        # Fall back to fixed stop sizing
        position_size = calculate_fixed_stop_position_size(
            account_equity=account_equity,
            risk_pct=settings.max_risk_per_trade_pct,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            max_position_pct=settings.max_ticker_allocation_pct,
        )
        dollar_risk = position_size * (signal.entry_price - signal.stop_loss)

    if settings.use_kelly_sizing and position_size > 0:
        position_size, kelly_multiplier, _ = apply_fractional_kelly(
            position_size=position_size,
            win_probability=signal.confidence,
            reward_risk_ratio=signal.risk_reward_ratio,
            scale=settings.kelly_fraction_scale,
            min_position_pct=settings.kelly_min_position_pct,
        )
        if position_size <= 0:
            return RiskDecision(
                approved=False,
                reason="kelly edge non-positive",
                position_size=0,
                dollar_risk=0.0,
            )
        dollar_risk = dollar_risk * kelly_multiplier

    exposure_warning: str | None = None
    if (
        avg_correlation is not None
        and max_avg_correlation is not None
        and max_avg_correlation > 0
        and position_size > 0
    ):
        reject_threshold = min(0.95, max_avg_correlation + 0.3)
        if avg_correlation >= reject_threshold:
            return RiskDecision(
                approved=False,
                reason=f"portfolio correlation too high ({avg_correlation:.2f} > {max_avg_correlation:.2f})",
                position_size=0,
                dollar_risk=0.0,
            )
        if avg_correlation > max_avg_correlation:
            overage = min(1.0, (avg_correlation - max_avg_correlation) / (1.0 - max_avg_correlation))
            corr_multiplier = max(0.25, 1.0 - overage)
            position_size = max(1, int(position_size * corr_multiplier))
            dollar_risk = dollar_risk * corr_multiplier
            exposure_warning = (
                f"portfolio correlation elevated ({avg_correlation:.2f} > {max_avg_correlation:.2f})"
            )
        elif avg_correlation >= max_avg_correlation * 0.8:
            exposure_warning = (
                f"portfolio correlation approaching limit ({avg_correlation:.2f} / {max_avg_correlation:.2f})"
            )

    # V3: Scale position size by counter-thesis confidence. A clean thesis
    # keeps full size (multiplier=1.0); a weak thesis takes a smaller cut.
    if counter_thesis is not None and position_size > 0:
        multiplier = max(0.0, min(1.0, counter_thesis.confidence_multiplier))
        position_size = max(1, int(position_size * multiplier))
        dollar_risk = dollar_risk * multiplier

    return RiskDecision(
        approved=position_size > 0,
        reason="approved" if position_size > 0 else "invalid size",
        position_size=position_size,
        dollar_risk=dollar_risk,
        portfolio_exposure_warning=exposure_warning,
    )
