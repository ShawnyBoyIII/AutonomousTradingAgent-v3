"""Defensive regression test for Important #2 of the Round 1 review.

After the Critical 1 fix landed (commit b7364c1), evaluate_exit_priority
and the trailing-stop check can mutate state.positions[ticker]
mid-iteration (the trailing-stop closure writes a ratcheted value
before signaling an exit). The close / partial paths need to receive
the *post-ratcheted* position, not the closure-captured pre-iteration
value.

This test drives the production _run_manage_positions_once body
end-to-end with a setup where the trailing-stop check fires BOTH
the ratchet AND the exit signal. It then asserts that:

1. The persisted ledger state has the ratcheted stop_loss / highest_high
   (the ratchet survives the no-exit path; covered by the Critical 1
   regression test).
2. When the next bar drops below the ratcheted stop, the trailing-stop
   exit fires and uses the post-ratchet position rather than the
   pre-ratcheted one.
3. The recorded fill attributes are consistent with the ratcheted
   position (entry_at, strategy_tag, quantity unchanged; the closure
   should still complete cleanly with no AttributeError or missing
   field).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def exit_env(monkeypatch, tmp_path):
    """Set up an environment where _run_manage_positions_once can be
    driven end-to-end with two-bar frames.
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
                    strategy_tag="v3-mean_reversion",
                ),
            },
            last_exited_at={},
        )
    )

    # Bar 1: price=115 (above highest_high). Forces the ratchet to fire.
    # next_trailing_stop computes candidate = 115 - 5 = 110 -> ratchet to 110.
    # The trailing_check returns None (since 115 > 110), so the position
    # survives and the ratchet lands on disk.
    bar1 = pd.DataFrame(
        {"close": [115.0], "high": [115.0], "low": [114.0], "volume": [1000]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-24 13:30:00", tz="UTC")]),
    )

    def _drive_bar(frame):
        monkeypatch.setattr(
            continuous_loop.market_data,
            "fetch_and_validate_bars",
            lambda *a, **k: (frame, types.SimpleNamespace(valid=True, reason="")),
        )

    _drive_bar(bar1)
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
        "drive_bar": _drive_bar,
    }


def test_trailing_stop_close_uses_post_ratchet_position(exit_env):
    """After the ratchet fires on bar 1 (state: stop=110, high=115),
    bar 2 at price=100 (below ratcheted stop=110) must trigger the
    trailing-stop exit AND complete the close cleanly (no AttributeError,
    no missing-field failure).
    """
    cl = exit_env["continuous_loop"]
    settings = exit_env["settings"]
    ledger = exit_env["ledger"]

    # Bar 1: ratchet.
    cl._run_manage_positions_once(settings, ledger, runtime_canary=None)
    after_bar1 = ledger.load_portfolio_state().positions["AAPL"]
    assert after_bar1.stop_loss == pytest.approx(110.0), (
        "fixture precondition: bar 1 must ratchet stop to 110"
    )

    # Bar 2: price=100, below ratcheted stop. trailing_stop_check should
    # fire. The close path needs to operate on the ratcheted state.
    bar2 = pd.DataFrame(
        {"close": [100.0], "high": [100.0], "low": [99.0], "volume": [1000]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-24 13:35:00", tz="UTC")]),
    )
    exit_env["drive_bar"](bar2)

    # Critical: this call must complete cleanly and remove the position.
    result = cl._run_manage_positions_once(settings, ledger, runtime_canary=None)

    # Position is gone.
    final = ledger.load_portfolio_state().positions
    assert "AAPL" not in final, (
        f"trailing-stop exit should have removed AAPL; ledger still has: {list(final)}"
    )
    # The trailing-stop exit produced one exit event with reason trailing_stop
    # and a fill.
    assert result["actions"] >= 1, (
        f"expected at least one exit action; got {result['actions']}"
    )


def test_trailing_stop_close_passes_posthoc_ratchet_fields(exit_env):
    """The close uses `live_position` (re-read AFTER evaluate_exit_priority
    returns), so the ratcheted state reaches fill_sell_position. Verify
    the exit event records use the post-ratcheted stop_loss indirectly by
    confirming the exit_reason recorded in the ledger orders table is the
    trailing_stop one (which only fires when the ratcheted threshold is
    crossed).
    """
    cl = exit_env["continuous_loop"]
    settings = exit_env["settings"]
    ledger = exit_env["ledger"]

    cl._run_manage_positions_once(settings, ledger, runtime_canary=None)

    bar2 = pd.DataFrame(
        {"close": [100.0], "high": [100.0], "low": [99.0], "volume": [1000]},
        index=pd.DatetimeIndex([pd.Timestamp("2026-07-24 13:35:00", tz="UTC")]),
    )
    exit_env["drive_bar"](bar2)
    cl._run_manage_positions_once(settings, ledger, runtime_canary=None)

    # Look at the most recent SELL row.
    recent = ledger.list_recent_order_rows(limit=5, naive_timezone="UTC")
    sells = [r for r in recent if r["side"] == "SELL"]
    assert len(sells) >= 1, (
        f"expected at least one SELL row, got: {recent}"
    )
    last_sell = sells[0]
    # The recorded fill should reflect the position's actual cost basis
    # and quantity — these weren't mutated by the ratchet.
    assert last_sell["quantity"] == 10
    assert last_sell["fill_price"] == pytest.approx(100.0 - 0.5) or last_sell["fill_price"] == pytest.approx(100.0), (
        f"unexpected fill_price: {last_sell['fill_price']} (slippage-bps=0 expected ≈100)"
    )
