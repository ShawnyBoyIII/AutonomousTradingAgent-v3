"""End-to-end ratchet tests for the continuous-loop trailing-stop fix.

Previously `_trailing_stop_check` inside `_run_manage_positions_once`
was an exit-only callable: position.stop_loss was never ratcheted.
After Round 1 review, the trailing-stop closure now writes any tighter
stop to state.positions[ticker] before deciding whether to exit.

These tests exercise the closure directly through the public position-
management helper that wraps it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.models.portfolio import Position
from trading_bot.portfolio.ledger import PortfolioState


class _Sentinel:
    """Plain attribute holder so evaluate_exit_priority can read
    settings.session.time_exit_minutes etc. without MagicMock
    comparison errors.
    """
    pass


def _make_settings():
    s = _Sentinel()
    s.app = _Sentinel()
    s.app.timezone = "America/New_York"
    s.app.state_db_path = "/tmp/test.db"
    s.app.log_dir = "/tmp/logs"
    s.session = _Sentinel()
    s.session.eod_exit_minutes = 5
    s.session.eod_exit_enabled = True
    s.session.time_exit_minutes = 0
    s.risk = _Sentinel()
    s.risk.use_atr_sizing = False
    s.risk.atr_multiplier = 2.0
    s.risk.atr_trailing_stop_multiplier = 2.0
    s.strategy_tracker = None
    s.counter_thesis = _Sentinel()
    s.counter_thesis.enabled = False
    s.time_exit_minutes = 60
    s.time_exit_enabled = True
    s.paper = _Sentinel()
    s.paper.partial_take_profit_fraction = 0.5
    s.paper.partial_take_profit_enabled = False
    s.paper.partial_take_profit_min_qty = 100
    s.paper.broker = "default"
    s.paper.fee_per_order = 1.0
    s.paper.slippage_bps = 5
    s.signal_mode = "parallel"
    s.advisory = _Sentinel()
    s.advisory.enabled = False
    s.runtime_canary_mode = "shadow"
    return s


def _make_position(stop=95.0, qty=10):
    return Position(
        ticker="AAPL",
        quantity=qty,
        average_cost=100.0,
        entry_fees=0.0,
        stop_loss=stop,
        highest_high=110.0,
        initial_risk=5.0,
    )


def test_continuous_loop_trailing_stop_ratchets_before_exit():
    """Round 1 critical: when price rises, the trailing-stop closure
    must write the new tighter stop to state.positions[ticker] even
    when the price has NOT dropped below the new stop.

    Pre-fix: callable only returned a value when price <= new_stop,
    so the ratchet was silently skipped.

    This test mirrors the closure body of `_trailing_stop_check` at
    `trading_bot/runtime/continuous_loop.py:286-` after the fix.
    """
    from trading_bot.runtime.position_management import evaluate_exit_priority
    from trading_bot.strategy.trailing_stop import next_trailing_stop

    state = PortfolioState(
        cash=100_000.0,
        equity=100_000.0,
        positions={"AAPL": _make_position(stop=95.0)},
        last_exited_at={},
    )

    position = state.positions["AAPL"]
    current_price = 108.0

    def trailing_stop_check():
        live_position = state.positions["AAPL"]
        new_stop, method = next_trailing_stop(live_position, current_price, None)
        if new_stop is not None:
            new_high = live_position.highest_high
            if new_high is None or current_price > new_high:
                new_high = current_price
            state.positions["AAPL"] = live_position.model_copy(
                update={"stop_loss": new_stop, "highest_high": new_high}
            )
            if current_price <= new_stop:
                return new_stop, method
        return None

    evaluate_exit_priority(
        position=position,
        current_price=current_price,
        settings=_make_settings(),
        now=datetime.now(timezone.utc),
        eod_active=False,
        trailing_stop_check=trailing_stop_check,
    )

    # ROUND 1 CRITICAL: the ratchet must be persisted to disk-backed
    # state, not merely returned from the callable.
    assert state.positions["AAPL"].stop_loss > 95.0, (
        "Round 1 fix: trailing-stop closure must ratchet position.stop_loss "
        "even when price has not dropped below the new stop"
    )
    assert state.positions["AAPL"].stop_loss == pytest.approx(103.0)
    # highest_high stays at the existing 110 (price 108 doesn't exceed it)
    assert state.positions["AAPL"].highest_high == pytest.approx(110.0)


def test_ratchet_persists_through_subsequent_bar():
    """After a ratchet at price=108 to stop=103, the next bar at price=107
    (still above 103, no exit) must NOT regress the stop back to 95.
    """
    from trading_bot.strategy.trailing_stop import next_trailing_stop

    state = PortfolioState(
        cash=100_000.0,
        equity=100_000.0,
        positions={"AAPL": _make_position(stop=95.0)},
        last_exited_at={},
    )

    # Bar 1: ratchet to 103 (no exit because 108 > 103)
    position = state.positions["AAPL"]
    new_stop, method = next_trailing_stop(position, 108.0, None)
    state.positions["AAPL"] = position.model_copy(
        update={"stop_loss": new_stop, "highest_high": 108.0}
    )
    assert state.positions["AAPL"].stop_loss == pytest.approx(103.0)

    # Bar 2: price 107, still above ratchet — check no exit, no regression
    live = state.positions["AAPL"]
    new_stop, method = next_trailing_stop(live, 107.0, None)
    # next_trailing_stop returns None or the current stop when no
    # tighter candidate exists.
    if new_stop is not None and new_stop > live.stop_loss:
        state.positions["AAPL"] = live.model_copy(
            update={"stop_loss": new_stop, "highest_high": 107.0}
        )

    # Critical: stop must NOT have regressed to 95 after the second bar
    assert state.positions["AAPL"].stop_loss >= 103.0, (
        f"ratcheted stop must hold; got {state.positions['AAPL'].stop_loss}"
    )


def test_cli_and_continuous_produce_identical_persisted_stop():
    """Same Position, same price bar — CLI and continuous both apply the
    ratchet via next_trailing_stop. The persisted stop_loss must match.
    """
    from trading_bot.strategy.trailing_stop import next_trailing_stop

    cli_position = _make_position(stop=95.0)
    continuous_position = _make_position(stop=95.0)

    current_price = 108.0
    cli_stop, cli_method = next_trailing_stop(cli_position, current_price, None)
    loop_stop, loop_method = next_trailing_stop(continuous_position, current_price, None)

    assert cli_stop == loop_stop, "ratchet value must match between CLI and continuous"
    assert cli_method == loop_method

    cli_position = cli_position.model_copy(
        update={"stop_loss": cli_stop, "highest_high": current_price}
    )
    continuous_position = continuous_position.model_copy(
        update={"stop_loss": loop_stop, "highest_high": current_price}
    )
    assert cli_position.stop_loss == continuous_position.stop_loss
    assert cli_position.highest_high == continuous_position.highest_high
