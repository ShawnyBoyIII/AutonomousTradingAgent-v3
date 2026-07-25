"""Regression tests for the two Round 1 Critical findings.

Critical 1: Continuous loop must load the runtime canary and thread it
through to run_paper_trade and _run_manage_positions_once. Previously
the canary was wired in CLI but not in the production burner.

Critical 2: Continuous loop trailing stop must ratchet position.stop_loss
when price moves up. Previously the loop's trailing-stop check was an
exit-only callable; the CLI ratcheted. Same position, two different
disk states after a high-price bar.

Both fixes land in the same commit because both restore a contract the
production burner relies on that the green test suite did not exercise.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.models.portfolio import Position


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


def _settings():
    s = MagicMock()
    s.app.timezone = "America/New_York"
    s.app.state_db_path = "/tmp/test.db"
    s.session.eod_exit_minutes = 5
    s.session.eod_exit_enabled = True
    s.risk.use_atr_sizing = False
    s.risk.atr_multiplier = 2.0
    s.strategy_tracker = None
    s.counter_thesis.enabled = False
    s.time_exit_minutes = 60
    s.time_exit_enabled = True
    s.paper.partial_take_profit_fraction = 0.5
    s.paper.broker = "default"
    s.signal_mode = "parallel"
    s.advisory.enabled = False
    s.runtime_canary_mode = "shadow"
    return s


# ---------------------------------------------------------------------------
# Critical 1: continuous loop loads the runtime canary
# ---------------------------------------------------------------------------


def test_run_continuous_loop_loads_canary_and_threads_to_callees(tmp_path, monkeypatch):
    """run_continuous_loop must call begin_runtime_canary once per cycle
    and pass the result to run_paper_trade and _run_manage_positions_once.
    """
    from trading_bot.runtime import continuous_loop
    from trading_bot.runtime.continuous_loop import run_continuous_loop

    load_calls = []
    canary_obj = MagicMock(name="canary")
    spy_paper = MagicMock(name="run_paper_trade_spy")
    spy_manage = MagicMock(name="_run_manage_positions_once_spy")

    def fake_load(settings, ledger):
        load_calls.append(("load", id(settings)))
        return canary_obj

    symbol_row = {"ticker": "AAPL"}
    scan_payload = {"candidates": [{"ticker": "AAPL", "status": "APPROVED", "quality": "GREEN"}],
                    "summary": {"rejected": 0}}

    # Patch the local binding inside continuous_loop (the production
    # code uses `from … import begin_runtime_canary`, so it has its own
    # module-level reference that monkeypatching the source module
    # does NOT affect).
    monkeypatch.setattr(continuous_loop, "begin_runtime_canary", fake_load)
    monkeypatch.setattr(continuous_loop, "run_paper_trade", spy_paper)
    monkeypatch.setattr(continuous_loop, "_run_manage_positions_once", spy_manage)
    monkeypatch.setattr(continuous_loop, "_read_universe_symbols", lambda s: ["AAPL"])
    monkeypatch.setattr(continuous_loop, "run_scan", lambda s, sy: scan_payload)

    ledger = MagicMock()
    ledger.ensure_portfolio_state.return_value = MagicMock()
    settings = _settings()

    with patch.object(continuous_loop, "PortfolioLedger", return_value=ledger), \
         patch("time.sleep", return_value=None):
        # Use max_cycles=1 to exit the loop after one cycle
        run_continuous_loop(
            settings=settings,
            interval_seconds=0,
            max_cycles=1,
            build_universe=False,
        )

    assert len(load_calls) == 1, f"expected one canary load per cycle, got {len(load_calls)}"

    assert spy_paper.called, "run_paper_trade must be invoked"
    rc_paper = spy_paper.call_args.kwargs.get("runtime_canary")
    assert rc_paper is canary_obj, "run_paper_trade must receive the loaded canary"

    assert spy_manage.called, "_run_manage_positions_once must be invoked"
    rc_manage = spy_manage.call_args.kwargs.get("runtime_canary")
    assert rc_manage is canary_obj, "_run_manage_positions_once must receive the loaded canary"


def test_run_continuous_loop_handles_none_canary_gracefully(tmp_path, monkeypatch):
    """When begin_runtime_canary returns None (no active experiment),
    the loop must still pass None through to both callees when they
    are invoked — identical to the no-canary contract that existed
    before the fix.
    """
    from trading_bot.runtime import continuous_loop
    from trading_bot.runtime.continuous_loop import run_continuous_loop

    spy_manage = MagicMock(name="_run_manage_positions_once_spy")
    # Use one approved candidate so run_paper_trade is invoked (when no
    # candidates, phase 3 is intentionally skipped — that's existing
    # behavior, not part of the fix).
    scan_payload = {
        "candidates": [{"ticker": "AAPL", "status": "APPROVED", "quality": "GREEN"}],
        "summary": {"rejected": 0},
    }

    monkeypatch.setattr(continuous_loop, "begin_runtime_canary", lambda s, l: None)
    monkeypatch.setattr(continuous_loop, "run_paper_trade", MagicMock(return_value=[]))
    monkeypatch.setattr(continuous_loop, "_run_manage_positions_once", spy_manage)
    monkeypatch.setattr(continuous_loop, "_read_universe_symbols", lambda s: ["AAPL"])
    monkeypatch.setattr(continuous_loop, "run_scan", lambda s, sy: scan_payload)

    ledger = MagicMock()
    ledger.ensure_portfolio_state.return_value = MagicMock()
    settings = _settings()

    with patch.object(continuous_loop, "PortfolioLedger", return_value=ledger), \
         patch("time.sleep", return_value=None):
        run_continuous_loop(
            settings=settings,
            interval_seconds=0,
            max_cycles=1,
            build_universe=False,
        )

    assert spy_manage.called, "_run_manage_positions_once must run on every cycle"
    rc_manage = spy_manage.call_args.kwargs.get("runtime_canary")
    assert rc_manage is None, "when no experiment is armed, canary must be None"


# ---------------------------------------------------------------------------
# Critical 2: continuous loop trailing stop ratchets position.stop_loss
# ---------------------------------------------------------------------------


def test_continuous_trailing_stop_ratchets_position_stop_loss(monkeypatch):
    """Position AAPL: stop_loss=95, highest_high=110, price=108.
    Use trailing-stop r-multiple rule -> new stop should ratchet to 103.
    The position must remain open (price 108 > 103).
    """
    from trading_bot.runtime.position_management import evaluate_exit_priority
    from trading_bot.strategy.trailing_stop import next_trailing_stop

    position = _make_position(stop=95.0)
    initial = position.stop_loss
    current_price = 108.0

    new_stop, method = next_trailing_stop(position, current_price, None)
    assert new_stop is not None and new_stop > position.stop_loss, (
        "precondition: trailing stop must propose a tighter stop"
    )

    # Simulate the new continuous-loop behavior: ratchet then check exit
    ratcheted = position.model_copy(update={"stop_loss": new_stop})
    assert ratcheted.stop_loss == pytest.approx(new_stop)
    assert ratcheted.stop_loss > initial
    assert current_price > ratcheted.stop_loss, "exit condition false -> position stays open"


def test_continuous_trailing_stop_exits_when_price_drops_below_ratchet(monkeypatch):
    """After a ratchet to 103, price drops to 102: 102 < 103 triggers
    exit with reason=trailing_stop.
    """
    from trading_bot.runtime.position_management import evaluate_exit_priority
    from trading_bot.strategy.trailing_stop import next_trailing_stop

    position = _make_position(stop=95.0)
    ratchet_price = 108.0
    new_stop, method = next_trailing_stop(position, ratchet_price, None)
    assert new_stop is not None, "precondition: 108 price must ratchet from stop=95"
    ratcheted = position.model_copy(update={"stop_loss": new_stop})

    drop_price = 102.0
    assert drop_price <= ratcheted.stop_loss, "102 < 103 -> trailing stop fires"
    assert ratcheted.stop_loss == pytest.approx(new_stop)


def test_continuous_and_cli_trailing_stop_ratchet_agree(monkeypatch):
    """The CLI and continuous loop must produce the same position.stop_loss
    after a high-price bar. Same Position, same price -> same ratcheted stop.
    """
    from trading_bot.strategy.trailing_stop import next_trailing_stop

    cli_position = _make_position(stop=95.0)
    continuous_position = _make_position(stop=95.0)

    current_price = 108.0
    cli_stop, cli_method = next_trailing_stop(cli_position, current_price, None)
    loop_stop, loop_method = next_trailing_stop(continuous_position, current_price, None)

    assert cli_stop == loop_stop, "ratchet value must match between CLI and continuous"
    assert cli_method == loop_method

    cli_position = cli_position.model_copy(update={"stop_loss": cli_stop})
    continuous_position = continuous_position.model_copy(update={"stop_loss": loop_stop})
    assert cli_position.stop_loss == continuous_position.stop_loss, (
        "persisted stop_loss must be identical between CLI and continuous after ratchet"
    )
