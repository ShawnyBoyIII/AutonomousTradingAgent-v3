from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

def calculate_position_size(
    account_equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
) -> int:
    equity = Decimal(str(account_equity))
    percentage = Decimal(str(risk_pct))
    entry = Decimal(str(entry_price))
    stop = Decimal(str(stop_loss))

    risk_per_share = entry - stop
    if equity <= 0 or percentage <= 0 or risk_per_share <= 0:
        return 0

    dollar_risk = equity * percentage
    shares = (dollar_risk / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)
    return int(shares)
