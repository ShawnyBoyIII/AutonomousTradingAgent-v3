from __future__ import annotations

from trading_bot.config.settings import RiskSettings
from trading_bot.models.risk import RiskDecision
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.position_sizer import calculate_position_size


def evaluate_signal(
    signal: TradeSignal,
    account_equity: float,
    open_tickers: set[str],
    risk_settings: RiskSettings | None = None,
) -> RiskDecision:
    settings = risk_settings or RiskSettings()

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

    position_size = calculate_position_size(
        account_equity=account_equity,
        risk_pct=settings.max_risk_per_trade_pct,
        entry_price=signal.entry_price,
        stop_loss=signal.stop_loss,
    )
    dollar_risk = position_size * (signal.entry_price - signal.stop_loss)

    return RiskDecision(
        approved=position_size > 0,
        reason="approved" if position_size > 0 else "invalid size",
        position_size=position_size,
        dollar_risk=dollar_risk,
    )
