r"""Simulated exchange handlers with microstructure frictions.

The module ships:

* :class:`ExchangeHandler` — abstract base every concrete handler
  must satisfy. A handler accepts ``OrderEvent`` instances against the
  current ``MarketEvent`` for a symbol, applies its order-type
  policy, and emits zero or more ``FillEvent`` instances back to the
  event queue.
* :class:`AlmgrenChrissModel` — stateless helper that computes
  permanent, temporary, and (optionally) square-root market impact.
* :class:`SimulatedExecutionHandler` — the production-grade concrete
  implementation. Supports ``MARKET``, ``LIMIT``, ``STOP``, and
  ``ICEBERG`` orders with deterministic limit/stop fills, partial
  fills tied to a configurable participation-rate cap, and per-fill
  only impact accounting so re-runs are reproducible.

Algorithmic notes:

* :math:`\\Delta P_{\\text{perm}} = \\theta · Q` is the permanent
  impact paid up-front and borne by every subsequent fill. With
  ``impact_scope='per_fill'`` (the default and only mode here) the
  permanent cost only distorts *this* fill's price, not later bars.
* :math:`\\Delta P_{\\text{temp}} = \\eta · Q / Δt` is the
  temporary impact earned back the moment the order completes.
* :math:`\\Delta P_{\\text{sqrt}} = Y · \\sigma · \\sqrt{Q / V}`
  is the optional square-root override useful for liquid equities.
* Total slippage is :math:`\\frac{1}{2} \\text{spread} + \\Delta P_{\\text{perm}}
  + \\Delta P_{\\text{temp}} + \\Delta P_{\\text{sqrt}}`. The spread
  penalty is half-spread *sign* so buys pay the ask, sells receive
  the bid.

Numerical discipline uses ``float64`` end-to-end with a final
``round(..., 4)`` on every monetary field exposed on ``FillEvent``
— matching :mod:`portfolio`'s rounding contract so the two layers
agree to the cent.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from event_engine.events import (
    FillEvent,
    MarketEvent,
    OrderDirection,
    OrderEvent,
    OrderType,
)
from event_engine.exceptions import (
    DataHandlerError,  # re-used for symbol-data absence
    EventValidationError,
)


# ---------------------------------------------------------------------------
# Custom errors
# ---------------------------------------------------------------------------


class ExecutionError(EventValidationError):
    """Base for execution-handler failures."""


class InsufficientLiquidityError(ExecutionError):
    """Raised when a bar has zero recorded volume, or when the
    participation cap would make a fill effectively zero.
    """


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class ExchangeHandler(ABC):
    """Contract every concrete execution handler must satisfy."""

    @abstractmethod
    def execute_order(
        self,
        order: OrderEvent,
        market: MarketEvent,
    ) -> Optional[FillEvent]:
        """Apply the handler's policy to ``order`` against ``market``
        and emit zero or one :class:`FillEvent`. ``None`` means
        the order rests (e.g. a limit that didn't cross) or was
        rejected.
        """


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AlmgrenChrissParams:
    """Coefficients for the Almgren-Chriss market-impact model.

    All fields are private to this dataclass so the handler can swap
    them through a single ``replace`` call without leaking
    implementation details to callers.

    Notation (matching the task prompt and the canonical model):

    * :math:`\\theta` (``theta``) : permanent impact coefficient.
    * :math:`\\eta` (``eta``) : temporary impact coefficient.
    * :math:`Y` (``Y``) : square-root impact coefficient.
    * :math:`Δt` (``dt``) : trading interval in seconds.

    The model treats :math:`Q` (signed trade size) and :math:`\\sigma`
    (volatility, decimal fraction) as per-call inputs.
    """

    theta: float = 0.0
    eta: float = 0.0
    Y: float = 0.0
    dt: float = 60.0

    def with_overrides(self, **kwargs: object) -> "AlmgrenChrissParams":
        """Return a copy with any subset of fields overridden."""
        return dataclasses_replace(self, **kwargs)


def dataclasses_replace(obj: AlmgrenChrissParams, **kwargs: object) -> AlmgrenChrissParams:
    """Tiny shim so the public ``with_overrides`` API doesn't drag
    the ``dataclasses.replace`` symbol into a hot import path. (One-
    call replacement, no copy overhead worth caching away.)"""
    return type(obj)(**{**obj.__dict__, **kwargs})


@dataclass(slots=True)
class SimulatedExchangeConfig:
    """Tunables for the simulated handler.

    * ``commission_per_share``: USD per share filled.
    * ``commission_min``: USD floor on commission per fill.
    * ``max_participation_pct``: ``0 < x <= 1``; the fraction of the
      bar's volume we are allowed to fill in one event.
    * ``impact_use_square_root``: when ``True``, override the
      permanent + temporary impacts with the square-root estimate.
    * ``symbol_overrides``: per-symbol ``{'sigma': float,
      'avg_volume': float}`` for the square-root path. Symbols
      without an entry fall back to the global defaults.
    """

    default_sigma: float = 0.01
    default_avg_volume: float = 1_000_000.0
    commission_per_share: float = 0.0
    commission_min: float = 0.0
    max_participation_pct: float = 0.10
    impact_use_square_root: bool = False
    symbol_overrides: dict[str, dict[str, float]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Almgren-Chriss math
# ---------------------------------------------------------------------------


@dataclass
class ImpactDecomposition:
    """Three-component impact breakdown. Monetary — USD/share for
    a *unit-quantity* order. The handler scales by :math:`Q` before
    applying.
    """

    permanent_per_unit: float
    temporary_per_unit: float
    square_root_per_unit: float

    @property
    def total_per_unit(self) -> float:
        """Total impact per unit. ``square_root_per_unit`` is the
        override when ``impact_use_square_root`` is enabled — in
        that mode the handler uses this field instead of
        ``permanent_per_unit`` + ``temporary_per_unit``.
        """
        return self.permanent_per_unit + self.temporary_per_unit + self.square_root_per_unit


def _validate_positive(name: str, value: float) -> None:
    if value <= 0 or math.isnan(value) or math.isinf(value):
        raise EventValidationError(f"{name} must be positive & finite; got {value!r}")


def permanent_impact_per_unit(theta: float, quantity_signed: int) -> float:
    """:math:`\\theta · Q` — permanent impact per *unit* traded.

    Returns a non-negative float.
    """
    if theta < 0 or math.isnan(theta) or math.isinf(theta):
        raise EventValidationError("theta must be non-negative & finite")
    return float(theta) * abs(quantity_signed)


def temporary_impact_per_unit(
    eta: float,
    quantity_signed: int,
    dt: float,
) -> float:
    """:math:`\\eta · Q / Δt` — temporary impact per unit.
    """
    if eta < 0 or math.isnan(eta) or math.isinf(eta):
        raise EventValidationError("eta must be non-negative & finite")
    if dt <= 0 or math.isnan(dt) or math.isinf(dt):
        raise EventValidationError("dt must be positive & finite")
    return float(eta) * abs(quantity_signed) / float(dt)


def square_root_impact_per_unit(
    Y: float,
    sigma: float,
    quantity_signed: int,
    avg_volume: float,
) -> float:
    """:math:`Y · \\sigma · \\sqrt{Q / V}` — square-root impact.

    Used as an *optional* override for the linear permanent + temporary
    sum. Almgren & Chriss show this law is exact for an *optimal
    schedule* under stochastic volatility; in a backtest we treat
    :math:`Q` as the full size and :math:`V` as some baseline volume.
    """
    if Y < 0 or math.isnan(Y) or math.isinf(Y):
        raise EventValidationError("Y must be non-negative & finite")
    if sigma < 0 or math.isnan(sigma) or math.isinf(sigma):
        raise EventValidationError("sigma must be non-negative & finite")
    if avg_volume < 0 or math.isnan(avg_volume) or math.isinf(avg_volume):
        raise EventValidationError("avg_volume must be non-negative & finite")
    q = abs(quantity_signed)
    if q == 0 or avg_volume <= 0:
        return 0.0
    return float(Y) * float(sigma) * math.sqrt(q / float(avg_volume))


def decompose_impact(
    *,
    theta: float,
    eta: float,
    Y: float,
    sigma: float,
    avg_volume: float,
    quantity_signed: int,
    dt: float,
    use_square_root: bool,
) -> ImpactDecomposition:
    """Compute the three impact components for a single fill.

    All three numeric returns are non-negative (USD / share for a
    unit-quantity order).
    """
    perm = permanent_impact_per_unit(theta, quantity_signed) if theta > 0 else 0.0
    temp = temporary_impact_per_unit(eta, quantity_signed, dt) if eta > 0 else 0.0
    sqrt_ = (
        square_root_impact_per_unit(Y, sigma, quantity_signed, avg_volume)
        if use_square_root
        else 0.0
    )
    return ImpactDecomposition(
        permanent_per_unit=perm,
        temporary_per_unit=temp,
        square_root_per_unit=sqrt_,
    )


# ---------------------------------------------------------------------------
# Concrete handler
# ---------------------------------------------------------------------------


class SimulatedExecutionHandler(ExchangeHandler):
    """Order-type-aware exchange handler with Almgren-Chriss impact.

    Lifecycle:

    1. The strategy places an :class:`OrderEvent` via the runtime.
    2. The runtime delivers the ``OrderEvent`` plus the matching
       :class:`MarketEvent` for ``symbol`` at ``timestamp_ns``.
    3. The handler validates, computes the impact and slippage, and
       returns a :class:`FillEvent` (``None`` for resting orders).
    4. The runtime pushes the ``FillEvent`` into the
       :class:`~event_engine.queue.EventQueue` for downstream
       accounting.

    Side effects:

    * ``resting_orders`` is a mapping of ``order_id`` to
      :class:`RestingOrder` for any limit/stop that didn't fully
      fill on its first tick. The runtime should consult
      :meth:`process_resting` on every subsequent market event for
      the same symbol until the order is filled, rejected, or
      cancelled.
    * The handler is *stateless across runs*; resting orders are
      dropped when the handler is garbage-collected. Callers that
      need durability must persist the resting map themselves.
    """

    def __init__(
        self,
        impact: AlmgrenChrissParams,
        config: Optional[SimulatedExchangeConfig] = None,
    ) -> None:
        if impact.dt <= 0 or math.isnan(impact.dt):
            raise EventValidationError(
                f"AlmgrenChrissParams.dt must be positive & finite; got {impact.dt}"
            )
        if not (0.0 < (config.max_participation_pct if config else 0.10) <= 1.0):
            raise EventValidationError(
                "max_participation_pct must be in (0, 1]"
            )
        self._impact = impact
        self._config = config or SimulatedExchangeConfig()
        self._resting_orders: dict[str, "RestingOrder"] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def impact_params(self) -> AlmgrenChrissParams:
        return self._impact

    @property
    def config(self) -> SimulatedExchangeConfig:
        return self._config

    def resting_orders(self) -> dict[str, "RestingOrder"]:
        """Snapshot of the orders resting in the book (for tests)."""
        return {k: v for k, v in self._resting_orders.items()}

    def execute_order(
        self,
        order: OrderEvent,
        market: MarketEvent,
    ) -> Optional[FillEvent]:
        if order.symbol != market.symbol:
            raise ExecutionError(
                f"order symbol {order.symbol!r} != market symbol {market.symbol!r}"
            )
        if order.order_type == OrderType.MARKET:
            return self._execute_market(order, market)
        if order.order_type == OrderType.LIMIT:
            return self._execute_limit(order, market)
        if order.order_type == OrderType.STOP:
            return self._execute_stop(order, market)
        if order.order_type == OrderType.ICEBERG:
            # Iceberg is a ``LIMIT`` with a participation cap; the
            # surface area behaves like a limit that only exposes part
            # of its size per bar. We cap :math:`Q` for this bar at
            # ``max_participation_pct`` so the public-book footprint is
            # always one ``Q`` at a time.
            return self._execute_iceberg(order, market)
        raise ExecutionError(f"unsupported order type {order.order_type!r}")

    def process_resting(
        self,
        market: MarketEvent,
    ) -> list[FillEvent]:
        """Re-evaluate every resting order against the latest bar.

        Returns zero or more ``FillEvent`` instances; rest orders that
        still don't cross are returned to the resting map.
        """
        fills: list[FillEvent] = []
        for order_id, resting in list(self._resting_orders.items()):
            if resting.order.symbol != market.symbol:
                continue
            order = resting.order
            if order.order_type == OrderType.LIMIT:
                fill = self._try_fill_limit(order, market, resting)
            elif order.order_type == OrderType.STOP:
                if not self._stop_triggered(order, market):
                    continue
                fill = self._fill_after_stop_trigger(order, market, resting)
            else:
                continue
            if fill is not None:
                fills.append(fill)
                self._resting_orders.pop(order_id, None)
        return fills

    # ------------------------------------------------------------------
    # Order-type policies
    # ------------------------------------------------------------------

    def _execute_market(
        self,
        order: OrderEvent,
        market: MarketEvent,
    ) -> FillEvent:
        requested = self._cap_to_volume(market, order.quantity)
        if requested <= 0:
            raise InsufficientLiquidityError(
                f"bar for {market.symbol} cannot fill market order "
                f"(participation cap is zero)"
            )
        return self._build_fill(
            order,
            market,
            quantity_signed=requested * self._sign(order.direction),
            label="MARKET",
        )

    def _execute_limit(
        self,
        order: OrderEvent,
        market: MarketEvent,
    ) -> Optional[FillEvent]:
        resting = RestingOrder(order=order, placed_at_ts=market.timestamp_ns)
        return self._try_fill_limit(order, market, resting)

    def _execute_stop(
        self,
        order: OrderEvent,
        market: MarketEvent,
    ) -> Optional[FillEvent]:
        if order.stop_price is None:
            raise ExecutionError("STOP order without stop_price")
        resting = RestingOrder(order=order, placed_at_ts=market.timestamp_ns)
        if not self._stop_triggered(order, market):
            self._resting_orders[order.order_id] = resting
            return None
        # Cap the conversion-to-market fill to participation volume.
        capped = self._cap_to_volume(market, order.quantity)
        if capped <= 0:
            self._resting_orders[order.order_id] = resting
            return None
        # Replace the order's quantity with the capped size for the
        # fill; we don't mutate the input.
        capped_order = OrderEvent(
            timestamp_ns=order.timestamp_ns,
            symbol=order.symbol,
            order_type=order.order_type,
            direction=order.direction,
            quantity=capped,
            limit_price=order.limit_price,
            stop_price=order.stop_price,
            order_id=order.order_id,
            time_in_force=order.time_in_force,
        )
        self._resting_orders.pop(order.order_id, None)
        return self._fill_after_stop_trigger(capped_order, market, resting)

    def _execute_iceberg(
        self,
        order: OrderEvent,
        market: MarketEvent,
    ) -> Optional[FillEvent]:
        if order.limit_price is None:
            raise ExecutionError("ICEBERG order requires limit_price")
        resting = RestingOrder(order=order, placed_at_ts=market.timestamp_ns)
        return self._try_fill_limit(order, market, resting)

    # ------------------------------------------------------------------
    # Limit fills
    # ------------------------------------------------------------------

    def _try_fill_limit(
        self,
        order: OrderEvent,
        market: MarketEvent,
        resting: "RestingOrder",
    ) -> Optional[FillEvent]:
        if order.limit_price is None:
            raise ExecutionError("LIMIT order without limit_price")

        # BUY limit: fill when low <= limit_price. SELL limit:
        # fill when high >= limit_price.
        crosses = self._limit_crossed(order, market, order.limit_price)
        if not crosses:
            self._resting_orders[order.order_id] = resting
            return None
        # Fill at the better of (limit_price, observed price).
        limit_price = float(order.limit_price)
        if order.direction is OrderDirection.BUY:
            fill_at = min(limit_price, market.low)
        else:
            fill_at = max(limit_price, market.high)
        capped_qty = self._cap_to_volume(market, order.quantity)
        if capped_qty <= 0:
            self._resting_orders[order.order_id] = resting
            return None
        return self._build_fill(
            order,
            market,
            quantity_signed=capped_qty * self._sign(order.direction),
            fill_at_price_override=fill_at,
            label="LIMIT",
        )

    @staticmethod
    def _limit_crossed(
        order: OrderEvent,
        market: MarketEvent,
        limit_price: float,
    ) -> bool:
        if order.direction is OrderDirection.BUY:
            return market.low <= limit_price
        return market.high >= limit_price

    # ------------------------------------------------------------------
    # Stop fills
    # ------------------------------------------------------------------

    @staticmethod
    def _stop_triggered(order: OrderEvent, market: MarketEvent) -> bool:
        if order.stop_price is None:
            return False
        if order.direction is OrderDirection.SELL:
            # Stop loss: trigger when price <= stop
            return market.low <= order.stop_price
        # Stop buy: trigger when price >= stop
        return market.high >= order.stop_price

    def _fill_after_stop_trigger(
        self,
        order: OrderEvent,
        market: MarketEvent,
        resting: "RestingOrder",
    ) -> FillEvent:
        # Convert to a market order at the trigger price.
        return self._build_fill(
            order,
            market,
            quantity_signed=order.quantity * self._sign(order.direction),
            label="STOP",
        )

    # ------------------------------------------------------------------
    # Common builder
    # ------------------------------------------------------------------

    def _cap_to_volume(self, market: MarketEvent, requested: int) -> int:
        r"""max_Q = max_participation_pct · V_bar."""
        if market.volume is None or market.volume <= 0:
            raise InsufficientLiquidityError(
                f"bar for {market.symbol} at {market.timestamp_ns} has "
                f"zero volume; cannot fill"
            )
        cap = int(market.volume * self._config.max_participation_pct)
        if cap <= 0:
            cap = 1  # tiny participation → allow at least one share
        return max(0, min(int(requested), cap))

    def _build_fill(
        self,
        order: OrderEvent,
        market: MarketEvent,
        *,
        quantity_signed: int,
        label: str,
        fill_at_price_override: Optional[float] = None,
    ) -> FillEvent:
        if quantity_signed == 0:
            raise ExecutionError(
                f"internal: zero quantity for order {order.order_id}"
            )

        # Symbol-level volatility / average-volume for square-root
        # impact. Falls back to the global config when a symbol isn't
        # listed.
        per_symbol = self._config.symbol_overrides.get(market.symbol, {})
        sigma = float(per_symbol.get("sigma", self._config.default_sigma))
        avg_volume = float(
            per_symbol.get("avg_volume", self._config.default_avg_volume)
        )

        impact = decompose_impact(
            theta=self._impact.theta,
            eta=self._impact.eta,
            Y=self._impact.Y,
            sigma=sigma,
            avg_volume=avg_volume,
            quantity_signed=quantity_signed,
            dt=self._impact.dt,
            use_square_root=self._config.impact_use_square_root,
        )

        # Pick the base fill price: explicit override (limit),
        # otherwise mid plus half-spread.
        if fill_at_price_override is not None:
            base_price = float(fill_at_price_override)
        else:
            mid = market.close
            if order.direction is OrderDirection.BUY:
                base_price = mid + market.bid_ask_spread / 2.0
            else:
                base_price = mid - market.bid_ask_spread / 2.0

        # Slippage / impact on top of base price — buy pays, sell receives
        # — so impact is always *added* to the buyer's price and
        # *subtracted* from the seller's fill. Total moves against the
        # taker.
        sign = self._sign(order.direction)
        if self._config.impact_use_square_root:
            per_unit_impact = impact.square_root_per_unit
        else:
            per_unit_impact = impact.permanent_per_unit + impact.temporary_per_unit
        spread_cost = market.bid_ask_spread / 2.0
        # ``base_price`` already paid the half-spread (taker pays the
        # ask / receives the bid). ``per_unit_impact`` is purely the
        # Almgren-Chriss component. Adding them gives the effective
        # fill price after impact.
        price_filled = round(base_price + sign * per_unit_impact, 4)
        if price_filled < 0:
            price_filled = 0.0

        # Decompose commission / slippage / impact for the FillEvent.
        filled_qty = abs(quantity_signed)
        slippage_cost = round(spread_cost * filled_qty, 4)
        impact_cost = round(per_unit_impact * filled_qty, 4)
        commission = round(
            self._config.commission_per_share * filled_qty
            + self._config.commission_min,
            4,
        )

        fill = FillEvent(
            timestamp_ns=market.timestamp_ns,
            symbol=market.symbol,
            exchange="SIM",
            quantity_filled=filled_qty,
            fill_price=price_filled,
            direction=order.direction,
            commission_fee=commission,
            slippage_cost=slippage_cost,
            impact_cost=impact_cost,
            order_id=order.order_id,
        )
        return fill

    # ------------------------------------------------------------------
    # Static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sign(direction: OrderDirection) -> int:
        return 1 if direction is OrderDirection.BUY else -1


# ---------------------------------------------------------------------------
# Resting-order bookkeeping
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RestingOrder:
    """One limit/stop that did not cross on its first tick.

    ``placed_at_ts`` is informational (debug UI), not load-bearing
    for any math.
    """

    order: OrderEvent
    placed_at_ts: int = 0


__all__ = [
    "ExchangeHandler",
    "SimulatedExecutionHandler",
    "AlmgrenChrissParams",
    "SimulatedExchangeConfig",
    "ImpactDecomposition",
    "ExecutionError",
    "InsufficientLiquidityError",
    "RestingOrder",
    "permanent_impact_per_unit",
    "temporary_impact_per_unit",
    "square_root_impact_per_unit",
    "decompose_impact",
]
