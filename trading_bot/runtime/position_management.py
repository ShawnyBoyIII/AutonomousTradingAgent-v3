"""Shared position-management evaluation.

The CLI `manage-positions` command and the continuous loop's
`_run_manage_positions_once` both implement the same exit priority
(EOD > stop > target > time_exit > counter-thesis > trailing stop).
This module is the canonical evaluator; both callers route through
it so exit reasons and persist records stay identical.

Canonical exit reasons (replacing the historical `eod` / `stop` /
`target` short forms and the `eod_exit` / `stop_loss` /
`profit_target` long forms):

    - "eod_exit"
    - "stop_loss"
    - "profit_target"
    - "time_exit_{minutes}m"
    - "counter_thesis"
    - "trailing_stop"
    - "no_exit" (returned when no priority fires)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trading_bot.models.portfolio import PortfolioState, Position

logger = logging.getLogger(__name__)


@dataclass
class ExitDecision:
    """Result of the exit-priority evaluation for a single position.

    ``reason`` is one of the canonical reasons listed in the module
    docstring. ``partial`` indicates the partial-take-profit path was
    taken; callers should call ``fill_partial_take_profit_position``
    instead of ``fill_sell_position`` in that case.
    """

    reason: str
    partial: bool = False
    skip_min_stop_widening: bool = False

    @property
    def should_exit(self) -> bool:
        return self.reason != "no_exit"


NO_EXIT = ExitDecision(reason="no_exit")


def evaluate_exit_priority(
    *,
    ticker: str,
    position: "Position",
    current_price: float,
    intraday_frame: Any,
    settings: Any,
    now: datetime,
    broker: Any,
    ledger: Any,
    state: "PortfolioState",
    log_path: Any,
    exit_events: list[dict],
    line_parts: list[str],
    runtime_canary: Any = None,
) -> tuple["PortfolioState", ExitDecision]:
    """Run the canonical exit-priority decision for a single position.

    The decision follows the documented ADR-001 priority order:

        EOD > stop > target > time_exit > counter-thesis > trailing stop

    Each priority branch either returns immediately with an updated
    state (after calling ``fill_sell_position`` /
    ``fill_partial_take_profit_position`` / ``_close_position``) or
    falls through to the next. The function returns the updated
    state and the ``ExitDecision`` describing which branch fired.
    Callers should ``continue`` their loop on any non-``no_exit``
    decision because the position has already been mutated.

    The caller is responsible for data freshness checks (5-minute
    staleness vs after-hours tolerance) — those differ between
    the CLI and the continuous loop and remain at the call site.
    """
    from trading_bot.runtime.position_exit import (
        fill_partial_take_profit_position,
        fill_sell_position,
    )
    from trading_bot.runtime.orchestrator import (
        _evaluate_counter_thesis_for_position,
    )
    from trading_bot.strategy.trailing_stop import next_trailing_stop
    from trading_bot.runtime.decision_log import append_decision_event

    # Exit priority 1: EOD
    from trading_bot.runtime.session import now_in_zone, should_eod_exit

    if should_eod_exit(now_in_zone(settings.app.timezone), settings.session):
        state, event, line = fill_sell_position(
            ticker=ticker,
            position=position,
            reason="eod_exit",
            submitted_at=now,
            last_price=current_price,
            broker=broker,
            ledger=ledger,
            state=state,
            log_path=log_path,
            exit_reason="eod_exit",
            settings=settings,
            runtime_canary=runtime_canary,
        )
        append_decision_event(log_path, event)
        exit_events.append(event)
        line_parts.append(line)
        return state, ExitDecision(reason="eod_exit")

    # Exit priority 2: Stop loss
    if position.stop_loss is not None and current_price <= position.stop_loss:
        state, event, line = fill_sell_position(
            ticker=ticker,
            position=position,
            reason="stop_loss",
            submitted_at=now,
            last_price=current_price,
            broker=broker,
            ledger=ledger,
            state=state,
            log_path=log_path,
            exit_reason="stop_loss",
            settings=settings,
            runtime_canary=runtime_canary,
        )
        append_decision_event(log_path, event)
        exit_events.append(event)
        line_parts.append(line)
        return state, ExitDecision(reason="stop_loss")

    # Exit priority 3: Profit target (with partial-take-profit branch)
    if position.profit_target is not None and current_price >= position.profit_target:
        if (
            settings.paper.partial_take_profit_enabled
            and not getattr(position, "partial_profit_taken", False)
            and position.quantity >= settings.paper.partial_take_profit_min_qty
        ):
            state, event, line = fill_partial_take_profit_position(
                ticker=ticker,
                position=position,
                submitted_at=now,
                last_price=current_price,
                broker=broker,
                ledger=ledger,
                state=state,
                log_path=log_path,
                fraction=settings.paper.partial_take_profit_fraction,
                settings=settings,
                runtime_canary=runtime_canary,
            )
            append_decision_event(log_path, event)
            exit_events.append(event)
            line_parts.append(line)
            return state, ExitDecision(reason="profit_target", partial=True)

        state, event, line = fill_sell_position(
            ticker=ticker,
            position=position,
            reason="profit_target",
            submitted_at=now,
            last_price=current_price,
            broker=broker,
            ledger=ledger,
            state=state,
            log_path=log_path,
            exit_reason="profit_target",
            settings=settings,
            runtime_canary=runtime_canary,
        )
        append_decision_event(log_path, event)
        exit_events.append(event)
        line_parts.append(line)
        return state, ExitDecision(reason="profit_target")

    # Exit priority 4: Time-based exit
    time_exit_m = settings.session.time_exit_minutes
    if time_exit_m > 0 and position.entry_at is not None:
        entry_at = position.entry_at
        if entry_at.tzinfo is None:
            entry_at = entry_at.replace(tzinfo=now.tzinfo)
        held = (now - entry_at).total_seconds() / 60.0
        if held >= time_exit_m:
            reason = f"time_exit_{int(held)}m"
            state, event, line = fill_sell_position(
                ticker=ticker,
                position=position,
                reason=reason,
                submitted_at=now,
                last_price=current_price,
                broker=broker,
                ledger=ledger,
                state=state,
                log_path=log_path,
                exit_reason=reason,
                settings=settings,
                runtime_canary=runtime_canary,
            )
            append_decision_event(log_path, event)
            exit_events.append(event)
            line_parts.append(line)
            return state, ExitDecision(reason=reason)

    # Exit priority 5: Counter-thesis (V3)
    if getattr(settings, "counter_thesis", None) is not None and settings.counter_thesis.enabled:
        result = _evaluate_counter_thesis_for_position(ticker, position, intraday_frame, settings)
        if result is not None and result.block_trade:
            state, event, line = fill_sell_position(
                ticker=ticker,
                position=position,
                reason="counter_thesis",
                submitted_at=now,
                last_price=current_price,
                broker=broker,
                ledger=ledger,
                state=state,
                log_path=log_path,
                exit_reason="counter_thesis",
                settings=settings,
                runtime_canary=runtime_canary,
            )
            event["counter_thesis"] = result.to_dict()
            append_decision_event(log_path, event)
            exit_events.append(event)
            line_parts.append(line)
            return state, ExitDecision(reason="counter_thesis")

    # Exit priority 6: Trailing stop
    from trading_bot.runtime.orchestrator import _fetch_atr

    atr = _fetch_atr(ticker, settings) if settings.risk.use_atr_sizing else None
    trailing_stop, _ = next_trailing_stop(position, current_price, atr)
    if trailing_stop is not None and current_price <= trailing_stop:
        state, event, line = fill_sell_position(
            ticker=ticker,
            position=position,
            reason="trailing_stop",
            submitted_at=now,
            last_price=current_price,
            broker=broker,
            ledger=ledger,
            state=state,
            log_path=log_path,
            exit_reason="trailing_stop",
            settings=settings,
            runtime_canary=runtime_canary,
        )
        append_decision_event(log_path, event)
        exit_events.append(event)
        line_parts.append(line)
        return state, ExitDecision(reason="trailing_stop")

    return state, NO_EXIT
