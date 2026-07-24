"""Pure, lazy exit-priority evaluation shared by position managers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ExitDecision:
    reason: str
    partial: bool = False
    payload: Any = None

    @property
    def should_exit(self) -> bool:
        return self.reason != "no_exit"


NO_EXIT = ExitDecision(reason="no_exit")


def evaluate_exit_priority(
    *,
    position: Any,
    current_price: float,
    settings: Any,
    now: datetime,
    eod_active: bool | None = None,
    counter_thesis_check: Callable[[], Any | None] | None = None,
    trailing_stop_check: Callable[[], Any | None] | None = None,
    state: Any = None,
    **_legacy_context: Any,
) -> ExitDecision | tuple[Any, ExitDecision]:
    """Return the first matching decision without performing side effects."""
    def _result(decision: ExitDecision) -> ExitDecision | tuple[Any, ExitDecision]:
        if state is not None:
            return state, decision
        return decision

    if eod_active is None:
        from zoneinfo import ZoneInfo

        from trading_bot.runtime.session import should_eod_exit

        eod_active = should_eod_exit(
            now.astimezone(ZoneInfo(settings.app.timezone)), settings.session
        )

    if eod_active:
        return _result(ExitDecision(reason="eod_exit"))

    if position.stop_loss is not None and current_price <= position.stop_loss:
        return _result(ExitDecision(reason="stop_loss"))

    if position.profit_target is not None and current_price >= position.profit_target:
        partial = (
            settings.paper.partial_take_profit_enabled
            and not getattr(position, "partial_profit_taken", False)
            and position.quantity >= settings.paper.partial_take_profit_min_qty
        )
        return _result(ExitDecision(reason="profit_target", partial=partial))

    time_exit_minutes = settings.session.time_exit_minutes
    if time_exit_minutes > 0 and position.entry_at is not None:
        entry_at = position.entry_at
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=now.tzinfo)
        held_minutes = (now - entry_at).total_seconds() / 60.0
        if held_minutes >= time_exit_minutes:
            return _result(ExitDecision(reason=f"time_exit_{int(held_minutes)}m"))

    if counter_thesis_check is not None:
        counter_result = counter_thesis_check()
        if counter_result is not None:
            return _result(ExitDecision(reason="counter_thesis", payload=counter_result))

    if trailing_stop_check is not None:
        trailing_result = trailing_stop_check()
        if trailing_result is not None:
            return _result(ExitDecision(reason="trailing_stop", payload=trailing_result))

    return _result(NO_EXIT)
