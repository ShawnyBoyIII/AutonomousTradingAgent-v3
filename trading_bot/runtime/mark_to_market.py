"""Mark-to-market equity helper.

Persisted ``PortfolioState.equity`` is currently computed at fill time
from ``cash + sum(qty * average_cost)`` and ``unrealized_pnl`` is
hard-coded to 0. The drawdown circuit breaker therefore never sees
adverse open-position moves until they exit.

This helper computes a NEW :class:`PortfolioState` whose ``equity``
and ``unrealized_pnl`` reflect current market prices supplied by the
caller. Cash and ``realized_pnl`` are preserved.

The helper is intentionally pure: it does not touch the ledger,
the broker, or any persistence layer. Callers persist the result.
"""

from __future__ import annotations

from typing import Mapping

from trading_bot.models.portfolio import PortfolioState


def mark_to_market(
    state: PortfolioState,
    prices: Mapping[str, float],
) -> PortfolioState:
    """Return a new state with equity and unrealized_pnl updated.

    Args:
        state: Existing portfolio state.
        prices: Map of ticker -> last close price. Missing tickers
            fall back to ``average_cost`` (no change in unrealized
            P&L) so the helper is safe to call when not every
            ticker's latest price is available.

    Returns:
        New :class:`PortfolioState` with refreshed equity and
        unrealized_pnl. Cash, realized_pnl, and position objects are
        otherwise unchanged.
    """
    new_unrealized = 0.0
    new_equity = float(state.cash)
    for ticker, position in state.positions.items():
        quantity = int(position.quantity)
        if quantity <= 0:
            continue
        price = float(prices.get(ticker, position.average_cost))
        new_equity += quantity * price
        new_unrealized += quantity * (price - float(position.average_cost))
    return state.model_copy(
        update={
            "equity": round(new_equity, 2),
            "unrealized_pnl": round(new_unrealized, 2),
        }
    )
