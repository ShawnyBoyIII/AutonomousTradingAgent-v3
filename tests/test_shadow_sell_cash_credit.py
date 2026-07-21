"""Shadow harness: SELL cash credit must use sold_qty, not fill.quantity.

Bug: shadow.py credited cash using ``fill.fill_price * fill.quantity`` even
when ``sold_qty`` was capped below ``fill.quantity`` (oversell / partial
fill). This created phantom cash and inflated shadow equity, breaking the
paired baseline used to evaluate candidate tuning changes.

Fix: cash credit uses ``fill.fill_price * sold_qty`` consistently with
realized P&L and position qty deduction.
"""

from __future__ import annotations

from pytest import approx

from trading_bot.learning.experiments.shadow import ShadowFill, ShadowLedger


def _make_shadow(tmp_path, starting_cash=10_000.0):
    return ShadowLedger(
        artifacts_dir=tmp_path,
        starting_cash=starting_cash,
    )


def test_oversell_does_not_credit_phantom_cash(tmp_path):
    """When fill.quantity exceeds position qty, cash must reflect only sold_qty."""
    harness = _make_shadow(tmp_path, starting_cash=9_000.0)

    # Open a 1-share long at avg cost 1,000 (cash drops to 8,000)
    harness._positions["AAPL"] = {"qty": 1.0, "cost_basis": 1_000.0}

    # SELL request 5 shares, but only 1 share is held.
    fill = ShadowFill(
        ticker="AAPL",
        side="SELL",
        quantity=5,  # requested more than held
        fill_price=1_100.0,
        fees=1.0,
    )
    harness.record(fill)

    # Cash: starting 9_000 + 1 share * 1,100 - 1 fee = 10_099
    # Pre-fix bug: 9_000 + 5 * 1,100 - 1 = 14_499 (phantom 4,400)
    assert harness._cash == approx(10_099.0), (
        f"cash must reflect only sold_qty=1 share, got {harness._cash}"
    )
    # Realized P&L: (1,100 - 1,000) * 1 - 1 = 99
    assert harness._closed_pnls[-1] == approx(99.0)
    # Position fully closed
    assert "AAPL" not in harness._positions


def test_partial_sell_credits_only_sold_quantity(tmp_path):
    """A 2-share position with a 1-share SELL credits exactly 1 share."""
    harness = _make_shadow(tmp_path, starting_cash=8_000.0)
    harness._positions["AAPL"] = {"qty": 2.0, "cost_basis": 2_000.0}

    fill = ShadowFill(
        ticker="AAPL",
        side="SELL",
        quantity=1,
        fill_price=1_200.0,
        fees=1.0,
    )
    harness.record(fill)

    # Cash: 8_000 + 1 * 1,200 - 1 = 9_199
    assert harness._cash == approx(9_199.0)
    # 1 share remaining
    assert harness._positions["AAPL"]["qty"] == 1.0
    # Cost basis reduced proportionally
    assert harness._positions["AAPL"]["cost_basis"] == approx(1_000.0)


def test_equity_curve_matches_cash_plus_remaining_position(tmp_path):
    """Mark-to-market equity must reconcile with cash + position value."""
    harness = _make_shadow(tmp_path, starting_cash=8_000.0)
    harness._positions["AAPL"] = {"qty": 2.0, "cost_basis": 2_000.0}

    fill = ShadowFill(
        ticker="AAPL",
        side="SELL",
        quantity=5,  # oversell: only 2 shares held
        fill_price=1_100.0,
        fees=1.0,
    )
    harness.record(fill)

    # Cash should be 8_000 + 2 * 1_100 - 1 = 10_199
    # No remaining position
    # Equity = cash = 10_199
    assert harness._cash == approx(10_199.0)
    assert "AAPL" not in harness._positions
    assert harness._equity_curve[-1] == approx(10_199.0)