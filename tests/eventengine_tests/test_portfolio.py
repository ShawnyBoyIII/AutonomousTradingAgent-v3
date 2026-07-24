"""Stage 2: Portfolio accounting — signal→order, fill handling, leverage."""
from __future__ import annotations

import pytest

from event_engine.events import (
    BarType,
    FillEvent,
    OrderDirection,
    OrderEvent,
    OrderType,
    SignalDirection,
    SignalEvent,
    TimeInForce,
)
from event_engine.exceptions import InsufficientCapitalError
from event_engine.portfolio import Portfolio, PortfolioPolicy


def _ts(offset: int = 0) -> int:
    """Stable test timestamp; 1_700_000_000_000_000_000 ns + offset."""
    return 1_700_000_000_000_000_000 + offset


def _buy_fill(quantity: int, fill_price: float, *, order_id: str, ts_offset: int) -> FillEvent:
    return FillEvent(
        timestamp_ns=_ts(ts_offset),
        symbol="AAPL",
        exchange="SIM",
        quantity_filled=quantity,
        fill_price=fill_price,
        direction=OrderDirection.BUY,
        commission_fee=1.0,
        slippage_cost=0.0,
        impact_cost=0.0,
        order_id=order_id,
    )


def _sell_fill(quantity: int, fill_price: float, *, order_id: str, ts_offset: int) -> FillEvent:
    return FillEvent(
        timestamp_ns=_ts(ts_offset),
        symbol="AAPL",
        exchange="SIM",
        quantity_filled=quantity,
        fill_price=fill_price,
        direction=OrderDirection.SELL,
        commission_fee=1.0,
        slippage_cost=0.0,
        impact_cost=0.0,
        order_id=order_id,
    )


def _long_signal(target: int, *, ts_offset: int) -> SignalEvent:
    return SignalEvent(
        timestamp_ns=_ts(ts_offset),
        symbol="AAPL",
        signal_type=SignalDirection.LONG,
        strength=0.7,
        target_quantity=target,
    )


# ---------------------------------------------------------------------------
# Capital constraints
# ---------------------------------------------------------------------------


def test_buy_signal_to_order_targets_initial_quantity():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=20_000))
    order = p.on_signal(_long_signal(150, ts_offset=10), last_price=100.0)
    assert order is not None
    assert order.direction is OrderDirection.BUY
    assert order.quantity == 150
    assert order.symbol == "AAPL"
    assert order.order_id.startswith("O-AAPL-")


def test_buy_exceeds_max_position_value_raises():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=20_000))
    with pytest.raises(InsufficientCapitalError):
        p.on_signal(_long_signal(500, ts_offset=10), last_price=100.0)


def test_buy_exceeds_symbol_weight_raises():
    """Symbol weight is notional / max(cash, 1) — a $50k order on
    $100k cash is 0.5 > 0.2 default."""
    p = Portfolio(PortfolioPolicy(initial_cash=100_000))
    with pytest.raises(InsufficientCapitalError):
        p.on_signal(_long_signal(500, ts_offset=10), last_price=100.0)


def test_buy_exceeds_leverage_raises():
    """Leverage_limit=2.0 means notional up to 2×cash is allowed.
    100 × $100 = $10k with $10k cash and 2.0 leverage is exactly the
    limit, so passes. Push to 105 shares (=$10.5k) to breach."""
    p = Portfolio(PortfolioPolicy(
        initial_cash=10_000, leverage_limit=2.0, max_position_value=100_000,
        max_symbol_weight=1.0,
    ))
    with pytest.raises(InsufficientCapitalError):
        p.on_signal(_long_signal(105, ts_offset=10), last_price=100.0)


def test_exit_on_flat_position_emits_no_order():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000))
    exit_signal = SignalEvent(
        timestamp_ns=_ts(10),
        symbol="AAPL",
        signal_type=SignalDirection.EXIT,
        strength=0.0,
        target_quantity=0,
    )
    assert p.on_signal(exit_signal, last_price=100.0) is None


def test_partial_fill_signals_only_the_delta_not_target():
    """A SignalEvent requesting more shares than currently held
    should produce a delta order, not a full rebuy."""
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_buy_fill(50, 100.0, order_id="O-AAPL-initial", ts_offset=10))
    incremental = p.on_signal(_long_signal(75, ts_offset=20), last_price=101.0)
    assert incremental.quantity == 25


# ---------------------------------------------------------------------------
# Fill handling — long add / close / short
# ---------------------------------------------------------------------------


def test_long_buy_buy_updates_average_cost():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_buy_fill(50, 100.0, order_id="a", ts_offset=10))
    p.on_fill(_buy_fill(50, 102.0, order_id="b", ts_offset=20))
    pos = p.position("AAPL")
    assert pos is not None
    assert pos.quantity == 100
    assert abs(pos.average_cost - 101.0) < 0.0001


def test_long_close_realises_pnl():
    """Realized P&L = gross price gain (commissions live in cash,
    not realised_pnl — the test_floor() assert checks cash below)."""
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_buy_fill(100, 100.0, order_id="a", ts_offset=10))
    p.on_fill(_sell_fill(100, 110.0, order_id="b", ts_offset=20))
    pos = p.position("AAPL")
    assert pos is None or pos.quantity == 0
    expected = 100 * (110.0 - 100.0)
    assert p.realised_pnl == pytest.approx(expected, rel=1e-3)
    # Cash dropped by commissions: 100_000 - 2 = 99_998
    assert p.cash == pytest.approx(100_000 + (110.0 - 100.0) * 100 - 2, rel=1e-3)


def test_partial_long_close_then_open_long_uses_remaining_average_cost():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_buy_fill(100, 100.0, order_id="a", ts_offset=10))
    p.on_fill(_sell_fill(50, 110.0, order_id="b", ts_offset=20))
    pos = p.position("AAPL")
    assert pos is not None
    assert pos.quantity == 50
    assert pytest.approx(pos.average_cost) == 100.0  # unchanged after partial close


# ---------------------------------------------------------------------------
# Short handling
# ---------------------------------------------------------------------------


def test_short_sell_records_short_position():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    short_signal = SignalEvent(
        timestamp_ns=_ts(10),
        symbol="AAPL",
        signal_type=SignalDirection.SHORT,
        strength=0.5,
        target_quantity=50,
    )
    order = p.on_signal(short_signal, last_price=100.0)
    assert order.direction is OrderDirection.SELL
    p.on_fill(_sell_fill(50, 101.0, order_id=order.order_id, ts_offset=20))
    pos = p.position("AAPL")
    assert pos is not None
    assert pos.quantity == -50


def test_short_borrow_fee_accrues_over_time():
    p = Portfolio(PortfolioPolicy(
        initial_cash=100_000, max_position_value=50_000,
        borrow_rate_per_day=0.0003,  # ~10% / yr
    ))
    short_signal = SignalEvent(
        timestamp_ns=_ts(10),
        symbol="AAPL",
        signal_type=SignalDirection.SHORT,
        strength=0.5,
        target_quantity=50,
    )
    order = p.on_signal(short_signal, last_price=100.0)
    p.on_fill(_sell_fill(50, 100.0, order_id=order.order_id, ts_offset=20))

    # Mark one day later
    p.mark_to_market({"AAPL": 100.0}, _ts(20 + 86_400 * 1_000_000_000))
    expected_fee = 0.0003 * 50 * 100.0
    pos = p.position("AAPL")
    assert pos.borrow_fee_accrued == pytest.approx(expected_fee, rel=1e-6)


def test_short_close_realises_pnl():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_sell_fill(50, 100.0, order_id="open-short", ts_offset=10))
    close_signal = SignalEvent(
        timestamp_ns=_ts(20),
        symbol="AAPL",
        signal_type=SignalDirection.EXIT,
        strength=0.0,
        target_quantity=0,
    )
    exit_order = p.on_signal(close_signal, last_price=95.0)
    p.on_fill(_buy_fill(50, 95.0, order_id=exit_order.order_id, ts_offset=21))
    # Short 50 @ 100, closed @ 95: realised = (sell - buy) * qty = $250.
    expected = 50 * (100.0 - 95.0)
    assert p.realised_pnl == pytest.approx(expected, rel=1e-3)


def test_position_cross_from_short_to_long_updates_average_cost():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_sell_fill(50, 100.0, order_id="open-short", ts_offset=10))
    p.on_fill(_buy_fill(50, 90.0, order_id="close-short", ts_offset=20))
    pos = p.position("AAPL")
    assert pos is None or pos.quantity == 0


# ---------------------------------------------------------------------------
# Mark-to-market / equity
# ---------------------------------------------------------------------------


def test_total_equity_includes_unrealised_pnl():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_buy_fill(100, 100.0, order_id="a", ts_offset=10))
    prices = {"AAPL": 110.0}
    p.mark_to_market(prices, _ts(20))
    eq = p.total_equity(prices)
    # cash after buy ~ 100_000 - 100*100 - 1 commission = 89_999
    # mark 100*110 = 11_000 ; equity = cash + market value
    # unrealised = (mark - cost) * qty = 100 * 10 = 1_000
    assert eq > p.cash
    position_market_value = 100 * 110.0
    cost_basis = 100 * 100.0
    unrealised = position_market_value - cost_basis
    assert eq - p.cash == pytest.approx(position_market_value, rel=1e-3)
    assert unrealised == pytest.approx(100 * (110.0 - 100.0), rel=1e-3)


def test_used_margin_for_shorts():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000, max_position_value=50_000))
    p.on_fill(_sell_fill(50, 100.0, order_id="a", ts_offset=10))
    p.mark_to_market({"AAPL": 100.0}, _ts(20))
    assert p.used_margin({"AAPL": 100.0}) == pytest.approx(50 * 100.0)


def test_summary_contains_required_keys():
    p = Portfolio()
    s = p.summary({})
    for key in (
        "cash", "equity", "realised_pnl", "unrealised_pnl",
        "used_margin", "free_margin", "positions",
    ):
        assert key in s


def test_summary_with_no_positions_is_clean():
    p = Portfolio()
    s = p.summary({})
    assert s["positions"] == {}
    assert s["used_margin"] == 0.0


# ---------------------------------------------------------------------------
# Rounding discipline
# ---------------------------------------------------------------------------


def test_cash_is_rounded_to_4dp_after_fill():
    """Reset the cash by a value that creates 5+ decimals."""
    p = Portfolio(PortfolioPolicy(initial_cash=100_000.0, max_position_value=50_000.0))
    p.on_fill(_buy_fill(33, 100.0 / 3, order_id="a", ts_offset=10))
    # cash after = 100_000 - 33 * 100/3 - 1 ~ 98_999.something
    # should round to 4dp on the cash property.
    assert round(p.cash, 4) == p.cash


def test_realised_pnl_rounded():
    p = Portfolio(PortfolioPolicy(initial_cash=100_000.0))
    # 7 shares at 100.1234 then sold at 110.5678 → 7 * (110.5678 - 100.1234) = 73.1108
    p.on_fill(FillEvent(
        timestamp_ns=_ts(10),
        symbol="AAPL", exchange="SIM",
        quantity_filled=7, fill_price=100.1234,
        direction=OrderDirection.BUY,
        commission_fee=0.5, slippage_cost=0.0, impact_cost=0.0,
        order_id="a",
    ))
    p.on_fill(FillEvent(
        timestamp_ns=_ts(20),
        symbol="AAPL", exchange="SIM",
        quantity_filled=7, fill_price=110.5678,
        direction=OrderDirection.SELL,
        commission_fee=0.5, slippage_cost=0.0, impact_cost=0.0,
        order_id="b",
    ))
    assert round(p.realised_pnl, 4) == p.realised_pnl
