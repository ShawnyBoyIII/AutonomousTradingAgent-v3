from __future__ import annotations


def exceeds_ticker_allocation(
    account_equity: float,
    position_value: float,
    max_allocation_pct: float,
) -> bool:
    return position_value > (account_equity * max_allocation_pct)
