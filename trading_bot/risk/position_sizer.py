from __future__ import annotations


def calculate_position_size(
    account_equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
) -> int:
    risk_per_share = entry_price - stop_loss
    if risk_per_share <= 0:
        return 0
    dollar_risk = account_equity * risk_pct
    return int(dollar_risk // risk_per_share)
