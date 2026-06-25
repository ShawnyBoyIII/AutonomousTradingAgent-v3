from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.portfolio.ledger import PortfolioLedger


class KillSwitchReason(Enum):
    """Reasons for kill switch activation."""

    MANUAL = "manual"
    EMERGENCY_STOP = "emergency_stop"
    CIRCUIT_BREAKER = "circuit_breaker"
    HOLIDAY = "holiday"
    EARLY_CLOSE = "early_close"
    SYSTEM_ERROR = "system_error"


@dataclass
class KillSwitchState:
    """Current state of the kill switch."""

    enabled: bool
    reason: str | None
    triggered_at: datetime | None
    triggered_by: str | None


def is_trading_halted(ledger: PortfolioLedger) -> KillSwitchState:
    """Check if trading is currently halted by kill switch.

    Args:
        ledger: Portfolio ledger with kill switch state

    Returns:
        KillSwitchState with current status
    """
    return ledger.get_kill_switch_state()


def halt_trading(
    ledger: PortfolioLedger,
    reason: str,
    triggered_by: str = "system",
) -> None:
    """Halt all trading activity.

    Args:
        ledger: Portfolio ledger
        reason: Reason for halting (manual, emergency, etc.)
        triggered_by: Who/what triggered the halt
    """
    ledger.set_kill_switch(
        enabled=True,
        reason=reason,
        triggered_by=triggered_by,
    )


def resume_trading(
    ledger: PortfolioLedger,
    resumed_by: str = "system",
) -> None:
    """Resume trading activity.

    Args:
        ledger: Portfolio ledger
        resumed_by: Who/what resumed trading
    """
    ledger.set_kill_switch(
        enabled=False,
        reason=None,
        triggered_by=resumed_by,
    )


def check_kill_switch_before_trade(ledger: PortfolioLedger) -> tuple[bool, str | None]:
    """Check if trading is allowed before executing a trade.

    Returns:
        Tuple of (allowed, reason_if_not_allowed)
    """
    state = is_trading_halted(ledger)

    if state.enabled:
        return False, f"Trading halted: {state.reason} (since {state.triggered_at})"

    return True, None
