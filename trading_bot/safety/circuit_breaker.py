"""Automated circuit breakers that auto-halt trading on catastrophic losses.

Phase 1 of the risk-management hardening layer.  When a circuit breaker
threshold is exceeded, ``check_circuit_breakers`` engages the kill switch
with ``KillSwitchReason.CIRCUIT_BREAKER`` so that *all* entry points
(scan, paper-trade, manage-positions) are blocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from trading_bot.monitoring.drawdown import compute_drawdown_from_ledger
from trading_bot.safety.kill_switch import KillSwitchReason, halt_trading

if TYPE_CHECKING:
    from trading_bot.config.settings import Settings
    from trading_bot.portfolio.ledger import PortfolioLedger


def check_circuit_breakers(
    ledger: PortfolioLedger,
    settings: Settings,
) -> tuple[bool, str | None]:
    """Check if any circuit breaker threshold is exceeded.

    Evaluates in this order (first match wins):

    1. **Kill switch already active** – return early so we don't re-trigger.
    2. **Consecutive losses** – if the most recent SELL orders are all
       losses and the count reaches ``max_consecutive_losses``, halt.
    3. **Drawdown** – if the max drawdown from the equity_history table
       reaches the ``monitoring.max_drawdown_pct`` limit, halt.

    When a breaker trips, ``halt_trading`` is called automatically so the
    kill-switch state persists in the ledger.  Both checks return the same
    ``(allowed, reason)`` contract as ``check_kill_switch_before_trade``.

    Args:
        ledger: Portfolio ledger (for order history + equity history).
        settings: Full settings (``risk`` and ``monitoring`` thresholds).

    Returns:
        ``(True, None)`` when no breaker trips.
        ``(False, reason)`` when a breaker tripped (kill switch engaged).
    """
    # If the kill switch is already active, echo its reason
    from trading_bot.safety.kill_switch import is_trading_halted

    ks = is_trading_halted(ledger)
    if ks.enabled:
        return False, f"Trading halted: {ks.reason} (since {ks.triggered_at})"

    # 1. Consecutive losses
    max_consecutive = getattr(settings.risk, "max_consecutive_losses", 5)
    if max_consecutive > 0:
        consecutive = ledger.get_consecutive_losses()
        if consecutive >= max_consecutive:
            reason = f"circuit breaker: {consecutive} consecutive losses"
            halt_trading(
                ledger,
                reason=reason,
                triggered_by=KillSwitchReason.CIRCUIT_BREAKER.value,
            )
            return False, reason

    # 2. Drawdown
    enable_dd = getattr(settings.risk, "enable_drawdown_circuit_breaker", True)
    if enable_dd:
        drawdown_metrics = compute_drawdown_from_ledger(ledger)
        max_drawdown = getattr(settings.monitoring, "max_drawdown_pct", 10.0)
        if drawdown_metrics.max_drawdown_pct >= max_drawdown:
            reason = (
                f"circuit breaker: max drawdown {drawdown_metrics.max_drawdown_pct:.2f}% "
                f"(limit {max_drawdown:.1f}%)"
            )
            halt_trading(
                ledger,
                reason=reason,
                triggered_by=KillSwitchReason.CIRCUIT_BREAKER.value,
            )
            return False, reason

    return True, None
