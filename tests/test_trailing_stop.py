from __future__ import annotations

from trading_bot.models.portfolio import Position
from trading_bot.strategy.trailing_stop import (
    chandelier_stop,
    next_trailing_stop,
    ratchet_stop,
)


def test_ratchet_stop_returns_current_when_price_below_one_r() -> None:
    assert ratchet_stop(current_stop=99.0, entry_price=100.0, last_price=100.5, initial_risk=1.0) == 99.0


def test_ratchet_stop_moves_to_breakeven_at_one_r() -> None:
    assert ratchet_stop(current_stop=99.0, entry_price=100.0, last_price=101.0, initial_risk=1.0) == 100.0


def test_ratchet_stop_trails_one_full_r_below_price() -> None:
    assert ratchet_stop(current_stop=99.0, entry_price=100.0, last_price=102.0, initial_risk=1.0) == 101.0
    assert ratchet_stop(current_stop=99.0, entry_price=100.0, last_price=105.0, initial_risk=1.0) == 104.0


def test_ratchet_stop_never_trails_down() -> None:
    # Current stop already ratcheted up; price falls but should not pull stop back.
    assert ratchet_stop(current_stop=101.0, entry_price=100.0, last_price=100.5, initial_risk=1.0) == 101.0
    assert ratchet_stop(current_stop=101.0, entry_price=100.0, last_price=101.5, initial_risk=1.0) == 101.0


def test_ratchet_stop_initializes_when_current_stop_missing() -> None:
    assert ratchet_stop(current_stop=None, entry_price=100.0, last_price=105.0, initial_risk=2.0) == 103.0


def test_ratchet_stop_returns_current_when_initial_risk_invalid() -> None:
    assert ratchet_stop(current_stop=99.0, entry_price=100.0, last_price=105.0, initial_risk=0.0) == 99.0


def test_chandelier_stop_subtracts_multiple_atr_from_highest_high() -> None:
    assert chandelier_stop(highest_high=120.0, atr=2.0, multiplier=1.5) == 117.0


def test_chandelier_stop_returns_none_when_atr_missing() -> None:
    assert chandelier_stop(highest_high=120.0, atr=None) is None


def test_chandelier_stop_returns_none_when_atr_non_positive() -> None:
    assert chandelier_stop(highest_high=120.0, atr=0.0) is None


def test_next_trailing_stop_picks_tighter_method() -> None:
    position = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        stop_loss=99.0,
        initial_risk=1.0,
        highest_high=120.0,
    )

    new_stop, method = next_trailing_stop(position, last_price=102.0, atr=2.0)

    # R-multiple: 102 - 1 = 101. Chandelier: 120 - (1.5 * 2) = 117. Tighter wins.
    assert new_stop == 117.0
    assert method == "chandelier-atr"


def test_next_trailing_stop_uses_ratchet_when_chandelier_below_current() -> None:
    position = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        stop_loss=99.0,
        initial_risk=1.0,
        highest_high=100.0,
    )

    new_stop, method = next_trailing_stop(position, last_price=105.0, atr=2.0)

    # R-multiple: 105 - 1 = 104. Chandelier: 100 - 3 = 97 (below current 99, skip).
    assert new_stop == 104.0
    assert method == "r-multiple"


def test_next_trailing_stop_returns_none_when_price_not_extended() -> None:
    position = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        stop_loss=99.0,
        initial_risk=1.0,
        highest_high=100.0,
    )

    new_stop, method = next_trailing_stop(position, last_price=100.5, atr=2.0)

    assert new_stop is None
    assert method is None


def test_next_trailing_stop_skips_ratchet_when_initial_risk_missing() -> None:
    position = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        stop_loss=99.0,
        initial_risk=None,
        highest_high=110.0,
    )

    new_stop, method = next_trailing_stop(position, last_price=105.0, atr=2.0)

    # Only chandelier fires: 110 - 3 = 107 (> 99).
    assert new_stop == 107.0
    assert method == "chandelier-atr"


def test_next_trailing_stop_returns_both_when_candidates_agree() -> None:
    position = Position(
        ticker="AAPL",
        quantity=10,
        average_cost=100.0,
        stop_loss=99.0,
        initial_risk=5.0,
        highest_high=110.0,
    )

    new_stop, method = next_trailing_stop(position, last_price=106.0, atr=6.0)

    # R-multiple: 106 - 5 = 101. Chandelier: 110 - (1.5 * 6) = 101. Tied.
    assert new_stop == 101.0
    assert method == "both"
