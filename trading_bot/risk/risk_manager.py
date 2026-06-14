from __future__ import annotations

from trading_bot.models.risk import RiskDecision
from trading_bot.models.signal import TradeSignal
from trading_bot.risk.position_sizer import calculate_position_size


def evaluate_signal(signal: TradeSignal, account_equity: float, open_tickers: set[str]) -> RiskDecision:
    if signal.risk_reward_ratio < 2.0:
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
        risk_pct=0.01,
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
