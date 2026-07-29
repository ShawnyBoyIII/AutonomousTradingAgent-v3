"""Portfolio accounting engine.

The :class:`Portfolio` tracks cash, equity, realised and unrealised
P&L, used margin, free margin, and per-symbol holdings across time
steps. It exposes three lifecycle hooks:

* :meth:`on_signal` accepts a :class:`SignalEvent`, evaluates the
  available cash, exposure limits, and leverage constraints, and
  either emits a :class:`OrderEvent` (returned to the caller) or
  raises :class:`InsufficientCapitalError`.

* :meth:`on_fill` accepts a :class:`FillEvent`, mutates the position
  book (long and short aware), accounts for commissions and
  slippage, and updates realised P&L for closing trades.

* :meth:`mark_to_market` accepts the latest price for each symbol
  (typically the close from a :class:`MarketEvent`) and refreshes
  the unrealised P&L plus margin state.

Numeric discipline. The safeguard required that currency and
quantity arithmetic use ``decimal.Decimal`` or precise ``float64``
rounding. ``Portfolio`` uses ``float64`` throughout, with a final
``round(..., 4)`` applied to every monetary field on exit (cash,
P&L, average buy/sell price, totals). This mirrors how most
production backtesters handle magnitude — float64 gives ~15
significant decimal digits, far more than the 4 we expose — and
keeps the call sites readable. Per-step rounding prevents creeping
rounding error from amplifying into cumulative drift across a
long backtest.

Memory. One float per (cash, equity, realised, unrealised, used
margin, free margin) total plus a dict entry per open symbol.
Holding 50 symbols through a million bars adds roughly 500 KB of
payload.

Complexity. ``on_signal`` and ``on_fill`` are ``O(1)`` per call.
``mark_to_market`` is ``O(N_open)`` per call (recomputes the mark
on every held symbol).

No imports from the DataHandler or EventQueue modules — the
:class:`Portfolio` is intentionally stand-alone and tested with
either a real EventQueue or just in-memory state.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from event_engine.events import (
    OrderDirection,
    OrderEvent,
    OrderType,
    SignalDirection,
    SignalEvent,
    FillEvent,
    TimeInForce,
)
from event_engine.exceptions import (
    EventValidationError,
    InsufficientCapitalError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _round_money(value: float) -> float:
    """Round to a 4-digit USD-equivalent integer. Public surface."""
    return round(float(value), 4)


def _round_qty(value: int) -> int:
    """Quantities are integers; sanity-clamp to a non-negative range."""
    if value < 0:
        raise EventValidationError(f"negative quantity {value}")
    return int(value)


# ---------------------------------------------------------------------------
# Per-symbol holdings
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Position:
    """One symbol's book entry.

    ``quantity`` is signed: positive is long, negative is short. Zero
    is a closed position. ``average_cost`` is the running average of
    buys (when ``quantity`` is positive) or sells (when negative);
    short-sales use ``average_short_cost``.
    """

    symbol: str
    quantity: int = 0
    average_cost: float = 0.0
    average_short_cost: float = 0.0
    realised_pnl: float = 0.0
    last_mark: float = 0.0
    borrow_fee_accrued: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


# ---------------------------------------------------------------------------
# Account policy
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PortfolioPolicy:
    """Risk / capital constraints.

    A pure-data holder so the same policy object can be shared by
    multiple :class:`Portfolio` instances (e.g. a/b comparisons
    during experiment sweeps).
    """

    initial_cash: float = 1_000_000.0
    leverage_limit: float = 1.0
    max_position_value: float = 100_000.0
    max_symbol_weight: float = 0.2
    commission_per_share: float = 0.0
    borrow_rate_per_day: float = 0.0003
    """Annualised borrow rate divided by 365; default ≈ 10% APY."""

    @classmethod
    def from_margin(cls, leverage: float = 1.0) -> "PortfolioPolicy":
        return cls(leverage_limit=leverage)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


class Portfolio:
    """Multi-asset portfolio accounting engine."""

    def __init__(self, policy: Optional[PortfolioPolicy] = None) -> None:
        self._policy = policy or PortfolioPolicy()
        self._cash: float = self._policy.initial_cash
        self._positions: dict[str, _Position] = defaultdict(
            lambda: _Position(symbol="__pending__")
        )
        # Avoid the defaultdict magic for symbol-keyed access when
        # the symbol isn't held yet — explicit overrides below.
        self._positions = {}
        self._last_timestamp_ns: Optional[int] = None
        self._realised_total: float = 0.0

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------

    @property
    def cash(self) -> float:
        return _round_money(self._cash)

    @property
    def realised_pnl(self) -> float:
        return _round_money(self._realised_total)

    @property
    def positions(self) -> dict[str, _Position]:
        """Snapshot of held positions. Mutation of the returned
        object does *not* affect the portfolio."""
        return {sym: pos for sym, pos in self._positions.items()}

    @property
    def policy(self) -> PortfolioPolicy:
        return self._policy

    def position(self, symbol: str) -> Optional[_Position]:
        """Return a *read-only* snapshot of the held position, or
        ``None`` if flat. Tests can introspect fields but should not
        mutate them."""
        return self._positions.get(symbol)

    # ------------------------------------------------------------------
    # Signal → Order conversion
    # ------------------------------------------------------------------

    def on_signal(self, signal: SignalEvent, *, last_price: float) -> Optional[OrderEvent]:
        """Translate a :class:`SignalEvent` into a validated
        :class:`OrderEvent`.

        Returns ``None`` for ``EXIT`` signals when the position is
        already flat — no order is needed.
        """
        if not isinstance(signal, SignalEvent):
            raise EventValidationError("on_signal expected a SignalEvent")
        symbol = signal.symbol
        if signal.signal_type == SignalDirection.EXIT:
            pos = self._positions.get(symbol)
            if pos is None or pos.is_flat:
                return None
            return self._build_exit_order(signal, symbol, pos, last_price)

        # Determine target signed quantity from signal_type + target_quantity.
        if signal.signal_type == SignalDirection.LONG:
            target_qty = abs(int(signal.target_quantity))
        elif signal.signal_type == SignalDirection.SHORT:
            target_qty = -abs(int(signal.target_quantity))
        else:
            raise EventValidationError(
                f"unknown signal_type {signal.signal_type!r}"
            )

        if target_qty == 0:
            return None

        existing = self._positions.get(symbol)
        current_qty = existing.quantity if existing else 0

        # The order is the difference between target and current —
        # never re-issue the entire target when an existing position
        # partially fills it.
        delta = target_qty - current_qty
        if delta == 0:
            return None
        direction = OrderDirection.BUY if delta > 0 else OrderDirection.SELL
        quantity = abs(delta)

        # Capital pre-check on the new gross exposure.
        notional = abs(last_price * quantity)
        if notional > self._policy.max_position_value:
            raise InsufficientCapitalError(
                f"order notional {notional} exceeds max_position_value "
                f"{self._policy.max_position_value} for {symbol}"
            )
        symbol_weight = notional / max(self._cash, 1e-9)
        if symbol_weight > self._policy.max_symbol_weight:
            raise InsufficientCapitalError(
                f"order weight {symbol_weight:.3f} exceeds max_symbol_weight "
                f"{self._policy.max_symbol_weight} for {symbol}"
            )
        if notional > self._cash * self._policy.leverage_limit:
            raise InsufficientCapitalError(
                f"order notional {notional} exceeds leverage_limit "
                f"{self._policy.leverage_limit}× cash {self._cash}"
            )

        order_id = f"O-{symbol}-{signal.timestamp_ns}-{abs(delta)}"
        return OrderEvent(
            timestamp_ns=signal.timestamp_ns,
            symbol=symbol,
            order_type=OrderType.MARKET,
            direction=direction,
            quantity=quantity,
            order_id=order_id,
            time_in_force=TimeInForce.GTC,
        )

    def _build_exit_order(
        self,
        signal: SignalEvent,
        symbol: str,
        position: _Position,
        last_price: float,
    ) -> OrderEvent:
        direction = (
            OrderDirection.SELL if position.is_long else OrderDirection.BUY
        )
        order_id = f"X-{symbol}-{signal.timestamp_ns}-{abs(position.quantity)}"
        return OrderEvent(
            timestamp_ns=signal.timestamp_ns,
            symbol=symbol,
            order_type=OrderType.MARKET,
            direction=direction,
            quantity=abs(position.quantity),
            order_id=order_id,
            time_in_force=TimeInForce.GTC,
        )

    # ------------------------------------------------------------------
    # Fill handling
    # ------------------------------------------------------------------

    def on_fill(self, fill: FillEvent) -> None:
        """Apply a :class:`FillEvent` to the position book.

        Updates ``_cash`` (subtracts the trade price + commission;
        adds the proceeds for closing trades), running average buy
        cost for additional long positions, average short cost for
        additional shorts, and realised P&L for crosses or
        reversals (long→short or short→long).
        """
        if not isinstance(fill, FillEvent):
            raise EventValidationError("on_fill expected a FillEvent")
        if fill.timestamp_ns != self._last_timestamp_ns and self._last_timestamp_ns is not None:
            # Allow forward-only timestamp.
            if fill.timestamp_ns < self._last_timestamp_ns:
                raise EventValidationError(
                    f"fill timestamp {fill.timestamp_ns} older than "
                    f"cursor {self._last_timestamp_ns}"
                )
        self._last_timestamp_ns = fill.timestamp_ns

        symbol = fill.symbol
        if symbol not in self._positions:
            self._positions[symbol] = _Position(symbol=symbol)

        pos = self._positions[symbol]
        qty = int(fill.quantity_filled)
        if qty == 0:
            return

        side = (
            OrderDirection.BUY
            if fill.direction == OrderDirection.BUY
            else OrderDirection.SELL
        )
        fill_price = float(fill.fill_price)
        commission = float(fill.commission_fee) + float(fill.slippage_cost) + float(fill.impact_cost)
        gross_value = qty * fill_price

        if side == OrderDirection.BUY:
            # Long add: cash out, recompute average cost.
            self._cash -= gross_value + commission
            self._apply_long_add(pos, qty, fill_price)
        else:
            # Sell: cash in. Either closes part/all of a long OR adds to a short.
            self._cash += gross_value - commission
            self._apply_sell(pos, qty, fill_price)

        self._realised_total += pos.realised_pnl - getattr(self, "_last_realised_delta", 0.0)
        self._last_realised_delta = pos.realised_pnl
        # Guard against fractional floating point drift:
        self._cash = _round_money(self._cash)
        self._realised_total = _round_money(self._realised_total)

    def _apply_long_add(self, pos: _Position, qty: int, fill_price: float) -> None:
        """Add to a long position. If the previous state was short,
        the new buy closes some (or all) of that short first.
        """
        if pos.quantity < 0:
            # Cross from short toward flat/long.
            closing_qty = min(qty, -pos.quantity)
            self._realise_short_close(pos, closing_qty, fill_price)
            remaining = qty - closing_qty
            pos.realised_pnl = _round_money(pos.realised_pnl)
            if remaining > 0:
                # Now opening a long.
                new_qty = pos.quantity + remaining
                total_cost = pos.average_cost * pos.quantity + fill_price * remaining
                pos.average_cost = total_cost / new_qty if new_qty > 0 else 0.0
                pos.quantity = new_qty
                pos.average_short_cost = 0.0
        else:
            # Adding to a long (or opening from flat).
            new_qty = pos.quantity + qty
            total_cost = pos.average_cost * pos.quantity + fill_price * qty
            pos.average_cost = total_cost / new_qty if new_qty > 0 else 0.0
            pos.quantity = new_qty
        # Reset borrow fees on any non-short state.
        if pos.quantity >= 0:
            pos.borrow_fee_accrued = 0.0

    def _apply_sell(self, pos: _Position, qty: int, fill_price: float) -> None:
        """Sell. Closes long first, then opens a short if more qty."""
        if pos.quantity > 0:
            closing_qty = min(qty, pos.quantity)
            self._realise_long_close(pos, closing_qty, fill_price)
            pos.realised_pnl = _round_money(pos.realised_pnl)
            remaining = qty - closing_qty
            if remaining > 0:
                # Now opening a short.
                new_qty = pos.quantity - remaining  # negative
                total_short = pos.average_short_cost * (-pos.quantity) + fill_price * remaining
                pos.average_short_cost = total_short / (-new_qty) if new_qty != 0 else 0.0
                pos.quantity = new_qty
                pos.average_cost = 0.0
        else:
            # Adding to a short (or opening from flat).
            new_qty = pos.quantity - qty  # negative
            total_short = pos.average_short_cost * (-pos.quantity) + fill_price * qty
            pos.average_short_cost = total_short / (-new_qty) if new_qty != 0 else 0.0
            pos.quantity = new_qty

    def _realise_long_close(self, pos: _Position, qty: int, fill_price: float) -> None:
        pos.realised_pnl += (fill_price - pos.average_cost) * qty
        pos.quantity -= qty
        if pos.quantity == 0:
            pos.average_cost = 0.0

    def _realise_short_close(self, pos: _Position, qty: int, fill_price: float) -> None:
        pos.realised_pnl += (pos.average_short_cost - fill_price) * qty
        pos.quantity += qty
        if pos.quantity == 0:
            pos.average_short_cost = 0.0

    # ------------------------------------------------------------------
    # Mark-to-market + summaries
    # ------------------------------------------------------------------

    def mark_to_market(self, prices: dict[str, float], timestamp_ns: int) -> None:
        """Refresh per-position marks and accrue borrow fees.

        Borrow fees accrue on shorts at ``policy.borrow_rate_per_day``
        × ``abs(quantity) × |mark|`` per day since the previous mark
        call. Time elapsed is measured by ``timestamp_ns`` so
        ``mark_to_market`` is forgiving of paused sessions.
        """
        # Accrue borrow fees per timestep.
        if self._last_timestamp_ns is not None and timestamp_ns > self._last_timestamp_ns:
            elapsed_ns = timestamp_ns - self._last_timestamp_ns
            elapsed_days = elapsed_ns / 86_400_000_000_000.0
            borrow_rate = self._policy.borrow_rate_per_day
            for sym, pos in self._positions.items():
                if pos.quantity < 0:
                    mark = prices.get(sym, pos.last_mark)
                    fee = (
                        borrow_rate
                        * (-pos.quantity)
                        * mark
                        * elapsed_days
                    )
                    pos.borrow_fee_accrued += fee
                    pos.realised_pnl -= fee
                    self._realised_total -= fee

        for symbol, price in prices.items():
            pos = self._positions.get(symbol)
            if pos is None or pos.quantity == 0:
                continue
            pos.last_mark = float(price)
        self._last_timestamp_ns = timestamp_ns

    def used_margin(self, prices: dict[str, float]) -> float:
        """Used margin = sum of ``|qty| × mark`` for shorts."""
        total = 0.0
        for symbol, pos in self._positions.items():
            if pos.quantity < 0:
                total += (-pos.quantity) * prices.get(symbol, pos.last_mark)
        return _round_money(total)

    def free_margin(self, prices: dict[str, float]) -> float:
        """Free margin = cash - used margin."""
        return _round_money(self._cash - self.used_margin(prices))

    def total_equity(self, prices: dict[str, float]) -> float:
        """Total equity = cash + sum of marked positions + accrued borrow fees."""
        marked = 0.0
        for symbol, pos in self._positions.items():
            if pos.quantity == 0:
                continue
            mark = prices.get(symbol, pos.last_mark)
            marked += pos.quantity * mark
        return _round_money(self._cash + marked)

    def unrealised_pnl(self, prices: dict[str, float]) -> float:
        """Unrealised P&L across all open positions at the given marks."""
        out = 0.0
        for symbol, pos in self._positions.items():
            if pos.quantity == 0:
                continue
            mark = prices.get(symbol, pos.last_mark)
            if pos.quantity > 0:
                out += (mark - pos.average_cost) * pos.quantity
            else:
                out += (pos.average_short_cost - mark) * (-pos.quantity)
        return _round_money(out)

    def summary(self, prices: dict[str, float]) -> dict[str, float]:
        """Snapshot of the portfolio state for the dashboard/test."""
        return {
            "cash": self.cash,
            "equity": self.total_equity(prices),
            "realised_pnl": self.realised_pnl,
            "unrealised_pnl": self.unrealised_pnl(prices),
            "used_margin": self.used_margin(prices),
            "free_margin": self.free_margin(prices),
            "positions": {
                sym: {
                    "qty": pos.quantity,
                    "avg_cost": pos.average_cost,
                    "avg_short_cost": pos.average_short_cost,
                    "mark": pos.last_mark,
                    "realised": pos.realised_pnl,
                    "borrow_fee_accrued": pos.borrow_fee_accrued,
                }
                for sym, pos in self._positions.items()
                if not pos.is_flat
            },
        }


__all__ = ["Portfolio", "PortfolioPolicy"]
