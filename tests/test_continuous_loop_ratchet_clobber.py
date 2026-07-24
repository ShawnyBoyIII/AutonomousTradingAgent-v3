"""Regression test for the trailing-stop ratchet clobber bug.

A Round-1 review of commit 970b181 ("fix(review): wire runtime canary
+ symmetric trailing-stop ratchet in continuous loop") found that the
trailing-stop ratchet the commit introduced is silently undone by the
no-exit cleanup at the bottom of _run_manage_positions_once:

    if position.highest_high is None or current_price > position.highest_high:
        updated_position = position.model_copy(update={"highest_high": current_price})
        state.positions[ticker] = updated_position

`position` here is a closure-captured local from the for-loop header.
When the trailing-stop ratchet inside _trailing_stop_check writes a new
state.positions[ticker] with stop_loss=ratcheted_value via model_copy,
that ratcheted stop_loss is forgotten on the next line because the
model_copy reuses the OLD position (with the original stop_loss).

The result is that the very bar that should PROTECT the position at a
tighter stop (because price exceeded the existing high) silently loses
that protection on the same iteration.

This test drives the actual _run_manage_positions_once body (not the
helper logic in isolation, which earlier regression tests covered) so
the no-exit clobber is exercised end-to-end.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def manage_positions_env(monkeypatch, tmp_path):
    """Set up an environment where _run_manage_positions_once can be
    driven end-to-end with one AAPL position and a synthetic bar.
    """
    import types

    from trading_bot.config.settings import Settings
    from trading_bot.models.portfolio import Position, PortfolioState
    from trading_bot.portfolio.ledger import PortfolioLedger
    from trading_bot.runtime import continuous_loop

    settings = Settings(
        app={
            "state_db_path": str(tmp_path / "state.db"),
            "log_dir": str(tmp_path / "logs"),
            "timezone": "America/New_York",
        }
    )
    settings.session.eod_enabled = False
    settings.risk.min_stop_distance_pct = 3.0
    settings.risk.use_atr_sizing = False
    settings.paper.fee_per_order = 1.0
    settings.paper.slippage_bps = 0
    settings.counter_thesis = None

    ledger = PortfolioLedger(tmp_path / "state.db")
    ledger.save_portfolio_state(
        PortfolioState(
            cash=9_000.0,
            equity=10_000.0,
            positions={
                "AAPL": Position(
                    ticker="AAPL",
                    quantity=10,
                    average_cost=100.0,
                    entry_fees=0.0,
                    stop_loss=95.0,
                    highest_high=110.0,
                    initial_risk=5.0,
                    entry_at=datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc),
                ),
            },
            last_exited_at={},
        )
    )

    # Synthetic 1-bar frame at price=115 (above highest_high=110).
    # The bar must be fresh (< 5 minutes old) or the staleness check
    # in ``_run_manage_positions_once`` skips the ticker before any
    # trailing-stop logic fires. Using ``now`` plus a tiny offset.
    bar_ts = pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=10)
    frame = pd.DataFrame(
        {"close": [115.0], "high": [115.0], "low": [114.0], "volume": [1000]},
        index=pd.DatetimeIndex([bar_ts]),
    )
    monkeypatch.setattr(
        continuous_loop.market_data,
        "fetch_and_validate_bars",
        lambda *a, **k: (frame, types.SimpleNamespace(valid=True, reason="")),
    )
    monkeypatch.setattr(
        "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
        lambda ledger: (True, ""),
    )
    monkeypatch.setattr(
        "trading_bot.safety.circuit_breaker.check_circuit_breakers",
        lambda ledger, settings: (True, ""),
    )

    return {
        "settings": settings,
        "ledger": ledger,
        "continuous_loop": continuous_loop,
    }


def test_trailing_stop_ratchet_survives_no_exit_high_update(manage_positions_env):
    """After _trailing_stop_check writes the ratchet, the no-exit
    cleanup must NOT overwrite state.positions[ticker].stop_loss with
    the closure-captured OLD stop_loss.
    """
    settings = manage_positions_env["settings"]
    ledger = manage_positions_env["ledger"]
    cl = manage_positions_env["continuous_loop"]

    result = cl._run_manage_positions_once(settings, ledger, runtime_canary=None)

    persisted_state = ledger.load_portfolio_state()
    position = persisted_state.positions["AAPL"]

    # The ratchet proposed stop_loss=110 (115 - 5). Pre-fix, the no-exit
    # cleanup clobbered it back to 95.
    assert position.stop_loss == pytest.approx(110.0), (
        f"trailing-stop ratchet clobbered by no-exit cleanup: "
        f"got stop_loss={position.stop_loss}, expected 110.0"
    )
    # highest_high also updates to current_price=115.
    assert position.highest_high == pytest.approx(115.0)
    # No exit triggered at price=115 (price > ratcheted stop=110).
    assert result["actions"] == 0


def test_ratchet_persists_through_subsequent_bar_unchanged(manage_positions_env):
    """After the ratchet survives iteration 1, a second bar at the same
    price must keep the ratcheted stop_loss (not regress it to the
    original).
    """
    settings = manage_positions_env["settings"]
    ledger = manage_positions_env["ledger"]
    cl = manage_positions_env["continuous_loop"]

    # First call: ratchet applied. Verify it sticks in the persisted state.
    cl._run_manage_positions_once(settings, ledger, runtime_canary=None)
    after_first = ledger.load_portfolio_state().positions["AAPL"].stop_loss
    assert after_first == pytest.approx(110.0)

    # Second call at price=115 again. With the fix, the ratchet reads
    # the persisted position and computes the SAME proposal (or tighter),
    # never regressing below 110.
    cl._run_manage_positions_once(settings, ledger, runtime_canary=None)
    after_second = ledger.load_portfolio_state().positions["AAPL"].stop_loss
    assert after_second >= 110.0, (
        f"ratcheted stop regressed between iterations: "
        f"{after_first} -> {after_second}"
    )
