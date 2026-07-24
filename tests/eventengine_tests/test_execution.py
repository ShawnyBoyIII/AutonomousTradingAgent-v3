"""Stage 3: execution handler — market impact, order type policies,
and FillEvent construction.
"""
from __future__ import annotations

import math
import time
from typing import Optional

import pytest

from event_engine.events import (
    FillEvent,
    MarketEvent,
    OrderDirection,
    OrderEvent,
    OrderType,
    TimeInForce,
)
from event_engine.exceptions import EventValidationError
from event_engine.execution import (
    AlmgrenChrissParams,
    ExchangeHandler,
    ImpactDecomposition,
    InsufficientLiquidityError,
    RestingOrder,
    SimulatedExchangeConfig,
    SimulatedExecutionHandler,
    decompose_impact,
    permanent_impact_per_unit,
    square_root_impact_per_unit,
    temporary_impact_per_unit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_ts() -> int:
    """A recent timestamp in UTC nanoseconds; tests round-trip the
    bar into the handler unchanged."""
    return int(time.time() * 1_000_000_000)


def _market(
    *,
    base: int,
    symbol: str = "AAPL",
    mid: float = 100.25,
    span: float = 0.75,        # half the bar range; high=mid+span, low=mid-span
    volume: float = 200_000.0,
    spread: float = 0.04,
    open_: float | None = None,
    close: float | None = None,
    high: float | None = None,
    low: float | None = None,
) -> MarketEvent:
    """Build a MarketEvent with OHLC consistently derived from ``mid``
    (default 100.25) and ``span`` (default 0.75). Pass explicit
    ``open_``, ``close``, ``high``, or ``low`` to override the defaults;
    bounds are widened automatically to keep the bar coherent."""
    effective_high = high if high is not None else mid + span
    effective_low = low if low is not None else mid - span
    if open_ is None:
        open_ = (effective_high + effective_low) / 2.0
    if close is None:
        close = (effective_high + effective_low) / 2.0
    # Final widening so all four are within [low, high] explicitly.
    lo = min(effective_low, open_, close)
    hi = max(effective_high, open_, close)
    return MarketEvent(
        timestamp_ns=base,
        symbol=symbol,
        open=open_,
        high=hi,
        low=lo,
        close=close,
        volume=volume,
        bid_ask_spread=spread,
    )


def _order(
    *,
    base: int,
    symbol: str = "AAPL",
    order_type: OrderType = OrderType.MARKET,
    direction: OrderDirection = OrderDirection.BUY,
    quantity: int = 1_000,
    order_id: str = "order-test",
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
) -> OrderEvent:
    return OrderEvent(
        timestamp_ns=base,
        symbol=symbol,
        order_type=order_type,
        direction=direction,
        quantity=quantity,
        order_id=order_id,
        limit_price=limit_price,
        stop_price=stop_price,
        time_in_force=TimeInForce.GTC,
    )


def _params(
    *,
    theta: float = 0.001,
    eta: float = 0.0001,
    Y: float = 0.0001,
    dt: float = 60.0,
) -> AlmgrenChrissParams:
    return AlmgrenChrissParams(theta=theta, eta=eta, Y=Y, dt=dt)


def _config(**overrides) -> SimulatedExchangeConfig:
    base = dict(
        default_sigma=0.01,
        default_avg_volume=5_000_000.0,
        commission_per_share=0.005,
        commission_min=1.0,
        max_participation_pct=0.10,
        impact_use_square_root=False,
        symbol_overrides={},
    )
    base.update(overrides)
    return SimulatedExchangeConfig(**base)


def _zero_impact_handler():
    """Handler with all impact coefficients zero. Useful when a test
    only cares about fill price *without* microstructure frictions.
    """
    return SimulatedExecutionHandler(
        AlmgrenChrissParams(theta=0.0, eta=0.0, Y=0.0, dt=60.0),
        _config(),
    )


def _handler(params: Optional[AlmgrenChrissParams] = None,
             config: Optional[SimulatedExchangeConfig] = None):
    return SimulatedExecutionHandler(
        params or _params(),
        config or _config(),
    )


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


def test_exchange_handler_is_abstract():
    with pytest.raises(TypeError):
        ExchangeHandler()


# ---------------------------------------------------------------------------
# Almgren-Chriss math
# ---------------------------------------------------------------------------


def test_permanent_impact_per_unit_matches_theta_times_q():
    assert permanent_impact_per_unit(theta=0.001, quantity_signed=1000) == pytest.approx(1.0)


def test_temporary_impact_per_unit_matches_eta_q_over_dt():
    val = temporary_impact_per_unit(eta=0.5, quantity_signed=1000, dt=60.0)
    assert val == pytest.approx(0.5 * 1000 / 60.0)
    # dt <= 0 still raises (division-by-zero protection). eta=0 is
    # valid (no temporary impact) and returns 0.
    with pytest.raises(EventValidationError):
        temporary_impact_per_unit(eta=0.5, quantity_signed=1000, dt=0.0)
    assert temporary_impact_per_unit(eta=0.0, quantity_signed=1000, dt=60.0) == 0.0


def test_square_root_impact_per_unit_basic():
    val = square_root_impact_per_unit(
        Y=0.314, sigma=0.01, quantity_signed=10_000, avg_volume=1_000_000
    )
    # Y * sigma * sqrt(Q/V) = 0.314 * 0.01 * sqrt(0.01) = 0.314e-3
    assert val == pytest.approx(0.314 * 0.01 * math.sqrt(10_000 / 1_000_000))


def test_square_root_impact_skips_when_invalid_inputs():
    assert square_root_impact_per_unit(Y=0.0, sigma=0.01, quantity_signed=100, avg_volume=1e6) == 0.0
    assert square_root_impact_per_unit(Y=0.1, sigma=0.01, quantity_signed=0, avg_volume=1e6) == 0.0


def test_decompose_returns_negative_unsigned_components():
    """Permanent, temporary, square-root are all *non-negative*."""
    d = decompose_impact(
        theta=0.001, eta=0.5, Y=0.1,
        sigma=0.01, avg_volume=1_000_000,
        quantity_signed=1000, dt=60.0,
        use_square_root=False,
    )
    assert isinstance(d, ImpactDecomposition)
    assert d.permanent_per_unit >= 0.0
    assert d.temporary_per_unit >= 0.0
    assert d.square_root_per_unit == 0.0
    assert d.total_per_unit == pytest.approx(d.permanent_per_unit + d.temporary_per_unit)


def test_decompose_with_square_root_uses_only_root():
    """When ``use_square_root=True`` the linear permanent + temporary
    terms are zero; only the square-root path contributes. (No-op
    when ``Y = 0``; that's a config decision, not a math outcome.)"""
    d = decompose_impact(
        theta=0.0, eta=0.0, Y=0.1,
        sigma=0.01, avg_volume=1_000_000,
        quantity_signed=1000, dt=60.0,
        use_square_root=True,
    )
    assert d.permanent_per_unit == 0.0
    assert d.temporary_per_unit == 0.0
    assert d.square_root_per_unit > 0.0
    assert d.total_per_unit == pytest.approx(d.square_root_per_unit)


def test_decompose_signed_negative_uses_absolute_value():
    """SELLs have negative quantity; impact uses |Q|."""
    d_buy = decompose_impact(
        theta=0.001, eta=0.0, Y=0.0,
        sigma=0.01, avg_volume=1_000_000,
        quantity_signed=1000, dt=60.0,
        use_square_root=False,
    )
    d_sell = decompose_impact(
        theta=0.001, eta=0.0, Y=0.0,
        sigma=0.01, avg_volume=1_000_000,
        quantity_signed=-1000, dt=60.0,
        use_square_root=False,
    )
    assert d_buy.permanent_per_unit == d_sell.permanent_per_unit


# ---------------------------------------------------------------------------
# Init / config validation
# ---------------------------------------------------------------------------


def test_constructor_rejects_zero_dt():
    with pytest.raises(EventValidationError):
        SimulatedExecutionHandler(
            AlmgrenChrissParams(theta=0.001, eta=0.1, Y=0.1, dt=0.0),
            _config(),
        )


def test_constructor_rejects_invalid_participation():
    with pytest.raises(EventValidationError):
        SimulatedExecutionHandler(
            _params(),
            _config(max_participation_pct=0.0),
        )
    with pytest.raises(EventValidationError):
        SimulatedExecutionHandler(
            _params(),
            _config(max_participation_pct=1.5),
        )


# ---------------------------------------------------------------------------
# Market orders
# ---------------------------------------------------------------------------


def test_market_buy_pays_spread_plus_permanent_plus_temporary():
    h = _handler(_params(theta=0.001, eta=0.0001, Y=0.0, dt=60.0))
    market = _market(base=_base_ts(), close=100.25, spread=0.04)
    order = _order(base=market.timestamp_ns, order_id="o1", quantity=1000)

    fill = h.execute_order(order, market)

    expected_perm = 0.001 * 1000
    expected_temp = 0.0001 * 1000 / 60.0
    half_spread = 0.02
    expected_base = 100.25 + half_spread
    expected_fill = round(expected_base + expected_perm + expected_temp, 4)
    assert fill.fill_price == pytest.approx(expected_fill)
    assert fill.direction is OrderDirection.BUY
    assert fill.quantity_filled == 1000
    assert fill.commission_fee == pytest.approx(0.005 * 1000 + 1.0)
    assert fill.slippage_cost == pytest.approx(half_spread * 1000)
    assert fill.impact_cost == pytest.approx((expected_perm + expected_temp) * 1000)
    assert fill.exchange == "SIM"
    assert fill.symbol == "AAPL"


def test_market_sell_receives_bid_minus_impact():
    h = _handler(_params(theta=0.001, eta=0.0, Y=0.0))
    market = _market(base=_base_ts(), close=100.25, spread=0.04)
    order = _order(
        base=market.timestamp_ns,
        direction=OrderDirection.SELL,
        order_id="o-sell",
        quantity=500,
    )
    fill = h.execute_order(order, market)
    # Sell receives bid side: mid - half-spread - permanent impact
    expected_base = 100.25 - 0.02
    expected_perm = 0.001 * 500
    assert fill.fill_price == pytest.approx(expected_base - expected_perm)


def test_market_order_caps_to_volume_cap():
    """A 1M-share market order on a 50k-volume bar is capped at 5k
    (10% participation default)."""
    h = _handler()  # max_participation_pct=0.10
    market = _market(base=_base_ts(), volume=50_000)
    order = _order(base=market.timestamp_ns, quantity=1_000_000)
    fill = h.execute_order(order, market)
    assert fill.quantity_filled == 5_000  # 10 % of 50k


def test_market_order_rejects_zero_volume_bar():
    h = _handler()
    market = _market(base=_base_ts(), volume=0.0)
    order = _order(base=market.timestamp_ns, quantity=100)
    with pytest.raises(InsufficientLiquidityError):
        h.execute_order(order, market)


# ---------------------------------------------------------------------------
# Limit orders
# ---------------------------------------------------------------------------


def test_limit_buy_no_fill_when_low_above_limit():
    """BUY limit at 98; bar low is 99.25 (above 98) so the limit
    does not cross and the order rests."""
    h = _zero_impact_handler()
    market = _market(base=_base_ts(), mid=99.75, span=0.5)  # low=99.25
    order = _order(
        base=market.timestamp_ns,
        order_type=OrderType.LIMIT,
        direction=OrderDirection.BUY,
        order_id="limit-buy",
        limit_price=98.0,
        quantity=500,
    )
    fill = h.execute_order(order, market)
    assert fill is None
    assert order.order_id in h.resting_orders()


def test_limit_buy_fills_at_min_of_limit_and_low():
    """BUY limit at 99; bar low 98. The handler fills at the
    better-of (limit, observed low) = min(99, 98) = 98. With zero
    impact configured the fill price is exactly 98."""
    h = _zero_impact_handler()
    market = _market(base=_base_ts(), mid=100.0, span=2.0)  # low=98
    order = _order(
        base=market.timestamp_ns,
        order_type=OrderType.LIMIT,
        direction=OrderDirection.BUY,
        order_id="limit-buy-cross",
        limit_price=99.0,
        quantity=200,
    )
    fill = h.execute_order(order, market)
    assert fill is not None
    assert fill.fill_price == pytest.approx(98.0)


def test_limit_buy_partial_fill_on_volume_cap():
    """Even after the price crosses, the fill is capped at the
    participation volume."""
    h = _handler()
    market = _market(base=_base_ts(), low=99.0, volume=5_000)
    order = _order(
        base=market.timestamp_ns,
        order_type=OrderType.LIMIT,
        direction=OrderDirection.BUY,
        order_id="limit-cap",
        limit_price=99.0,
        quantity=2_000,  # wants 2000
    )
    fill = h.execute_order(order, market)
    # 10% of 5000 = 500 capped; 500 returned
    assert fill.quantity_filled == min(500, 2000)
    assert order.order_id not in h.resting_orders()


def test_limit_sell_fills_at_max_of_limit_and_high():
    """SELL limit at 105; bar high 110. Fills at max(105, 110) = 110
    with zero impact configured."""
    h = _zero_impact_handler()
    market = _market(base=_base_ts(), mid=109.0, span=2.0)  # high=111
    order = _order(
        base=market.timestamp_ns,
        order_type=OrderType.LIMIT,
        direction=OrderDirection.SELL,
        order_id="limit-sell",
        limit_price=105.0,
        quantity=300,
    )
    fill = h.execute_order(order, market)
    assert fill is not None
    assert fill.fill_price == pytest.approx(111.0)  # > limit, = high


def test_limit_without_limit_price_raises():
    """A LIMIT OrderEvent with ``limit_price`` omitted is rejected at
    construction, before reaching the handler. ``SimulatedExecutionHandler``
    doesn't need to re-check."""
    with pytest.raises(EventValidationError):
        OrderEvent(
            timestamp_ns=_base_ts(),
            symbol="AAPL",
            order_type=OrderType.LIMIT,
            direction=OrderDirection.BUY,
            quantity=100,
            order_id="no-price",
        )


# ---------------------------------------------------------------------------
# Stop orders
# ---------------------------------------------------------------------------


def test_stop_sell_rests_until_low_crosses_trigger():
    h = _handler()
    market = _market(base=_base_ts(), low=101.0)
    stop_price = 100.0
    order = _order(
        base=market.timestamp_ns,
        order_type=OrderType.STOP,
        direction=OrderDirection.SELL,
        order_id="stop-sell",
        stop_price=stop_price,
        quantity=500,
    )
    # 101 > 100, stop didn't trigger, rests.
    assert h.execute_order(order, market) is None
    assert order.order_id in h.resting_orders()


def test_stop_buy_triggers_when_high_reaches_stop():
    """STOP BUY: trigger when high >= stop. Then convert to market."""
    h = _handler()
    market = _market(base=_base_ts(), high=110.0, close=109.0)
    order = _order(
        base=market.timestamp_ns,
        order_type=OrderType.STOP,
        direction=OrderDirection.BUY,
        order_id="stop-buy",
        stop_price=108.0,
        quantity=200,
    )
    fill = h.execute_order(order, market)
    # BUY market at mid + half-spread + permanent/temporary
    assert fill is not None
    assert fill.direction is OrderDirection.BUY
    # No more resting after a triggered fill
    assert order.order_id not in h.resting_orders()


# ---------------------------------------------------------------------------
# ICEBERG
# ---------------------------------------------------------------------------


def test_iceberg_is_a_limit_with_participation_cap():
    """ICEBERG without a fill from the first bar rests and waits
    for a later crossing bar."""
    h = _handler()
    market = _market(base=_base_ts(), low=100.5)  # stays above 99.9
    order = _order(
        base=market.timestamp_ns,
        order_type=OrderType.ICEBERG,
        direction=OrderDirection.BUY,
        order_id="iceberg",
        limit_price=99.9,
        quantity=10_000,
    )
    fill = h.execute_order(order, market)
    assert fill is None
    assert order.order_id in h.resting_orders()


# ---------------------------------------------------------------------------
# Symbol mismatch guard
# ---------------------------------------------------------------------------


def test_execute_rejects_symbol_mismatch():
    h = _handler()
    order = _order(base=_base_ts(), symbol="AAPL")
    market = _market(base=_base_ts(), symbol="MSFT")
    with pytest.raises(Exception):
        h.execute_order(order, market)


# ---------------------------------------------------------------------------
# Integration with EventQueue
# ---------------------------------------------------------------------------


def test_filled_event_round_trips_through_queue():
    """The execution handler produces a FillEvent; verify it survives
    round-trip through the EventQueue (which already accepts any
    Event subclass)."""
    from event_engine.queue import EventQueue

    h = _handler()
    queue = EventQueue()
    base = _base_ts()
    market = _market(base=base)
    order = _order(base=base, order_id="rt-1")
    fill = h.execute_order(order, market)
    queue.put(fill)
    popped = queue.get()
    assert popped.order_id == fill.order_id
    assert popped.fill_price == fill.fill_price


# ---------------------------------------------------------------------------
# Resting orders
# ---------------------------------------------------------------------------


def test_process_resting_emits_fills_only_after_cross():
    """A resting limit fills on a later bar that crosses."""
    h = _zero_impact_handler()
    # Bar 1: limit doesn't cross, order rests.
    base = _base_ts()
    bar1 = _market(base=base, mid=101.0, span=1.0)  # low=100, high=102
    order = _order(
        base=base,
        order_type=OrderType.LIMIT,
        direction=OrderDirection.BUY,
        order_id="resting",
        limit_price=99.0,
        quantity=200,
    )
    assert h.execute_order(order, bar1) is None
    assert "resting" in h.resting_orders()

    # Bar 2: low drops to 98, crosses the 99 limit.
    bar2 = _market(base=base + 60_000_000_000, mid=99.0, span=1.0, volume=10_000)
    fills = h.process_resting(bar2)
    assert len(fills) == 1
    assert fills[0].fill_price == pytest.approx(98.0)
    assert "resting" not in h.resting_orders()


def test_process_resting_skips_other_symbols():
    """Resting orders for other symbols stay in the book."""
    h = _handler()
    base = _base_ts()
    aapl_bar = _market(base=base, low=100.0)
    aapl_order = _order(
        base=base, symbol="AAPL", order_type=OrderType.LIMIT,
        direction=OrderDirection.BUY, order_id="aapl-1",
        limit_price=99.0, quantity=100,
    )
    msft_order = OrderEvent(
        timestamp_ns=base,
        symbol="MSFT",
        order_type=OrderType.LIMIT,
        direction=OrderDirection.BUY,
        quantity=100,
        order_id="msft-1",
        limit_price=99.0,
    )
    assert h.execute_order(aapl_order, aapl_bar) is None
    # Inject an MSFT resting order directly so process_resting is the
    # only trigger; we expect it to stay put on an AAPL bar.
    h._resting_orders["msft-1"] = RestingOrder(order=msft_order, placed_at_ts=base)

    fills = h.process_resting(aapl_bar)
    assert fills == []
    assert "msft-1" in h.resting_orders()


def test_resting_order_stops_when_stop_triggers():
    """A STOP that triggers on a later bar converts to a market fill
    on that bar."""
    h = _handler()
    base = _base_ts()
    bar1 = _market(base=base, low=101.0)
    stop = _order(
        base=base, order_type=OrderType.STOP,
        direction=OrderDirection.SELL, order_id="stop-resty",
        stop_price=100.0, quantity=200,
    )
    assert h.execute_order(stop, bar1) is None
    assert "stop-resty" in h.resting_orders()

    bar2 = MarketEvent(
        timestamp_ns=base + 60_000_000_000,
        symbol="AAPL",
        open=99.5, high=100.5, low=99.0, close=99.0,
        volume=10_000, bid_ask_spread=0.04,
    )
    fills = h.process_resting(bar2)
    assert len(fills) == 1
    assert fills[0].direction is OrderDirection.SELL
    assert "stop-resty" not in h.resting_orders()


# ---------------------------------------------------------------------------
# Per-symbol config and square-root toggle
# ---------------------------------------------------------------------------


def test_symbol_override_changes_avg_volume_for_square_root():
    h = _handler(
        _params(theta=0.0, eta=0.0, Y=0.314, dt=60.0),
        _config(
            impact_use_square_root=True,
            symbol_overrides={"AAPL": {"sigma": 0.01, "avg_volume": 4_000_000}},
        ),
    )
    market = _market(base=_base_ts())  # mid=100.25, spread=0.04
    order = _order(base=market.timestamp_ns, quantity=1_000, order_id="sqrt")
    fill = h.execute_order(order, market)
    expected_root = 0.314 * 0.01 * math.sqrt(1_000 / 4_000_000)
    # Buy side: half-spread on top of mid, then root impact.
    # Per-symbol override gives avg_volume=4_000_000, sigma=0.01.
    expected_fill = round(
        100.25 + 0.02 + round(expected_root, 4), 4
    )
    assert fill.fill_price == pytest.approx(expected_fill, rel=1e-3)
    assert fill.impact_cost == pytest.approx(round(expected_root * 1_000, 4), rel=1e-3)


def test_default_symbol_falls_back_to_global_overrides():
    h = _handler(
        _params(theta=0.0, eta=0.0, Y=0.314, dt=60.0),
        _config(
            impact_use_square_root=True,
            default_sigma=0.02,  # global override
            default_avg_volume=8_000_000,
        ),
    )
    market = _market(base=_base_ts(), symbol="UNKNOWN")
    order = _order(base=market.timestamp_ns, symbol="UNKNOWN", quantity=2_000, order_id="def")
    fill = h.execute_order(order, market)
    expected_root = 0.314 * 0.02 * math.sqrt(2_000 / 8_000_000)
    expected_fill = round(
        market.close + market.bid_ask_spread / 2.0 + expected_root, 4
    )
    assert fill.fill_price == pytest.approx(expected_fill)


# ---------------------------------------------------------------------------
# Rounding discipline
# ---------------------------------------------------------------------------


def test_filled_price_is_rounded_to_4dp():
    h = _handler(_params(theta=0.000123, eta=0.000456))
    market = _market(base=_base_ts(), close=100.123456, spread=0.123456)
    order = _order(base=market.timestamp_ns, order_id="rnd", quantity=100)
    fill = h.execute_order(order, market)
    # fill_price must be at most 4 decimal places.
    assert len(str(fill.fill_price).rsplit(".", 1)[-1]) <= 4
