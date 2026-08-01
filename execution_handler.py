import math
from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from decimal import Decimal

from event_infrastructure import (
    EventQueue, Event, MarketEvent, OrderEvent, FillEvent,
    OrderType, OrderDirection
)
from market_and_portfolio import AbstractDataHandler

class AbstractExecutionHandler(ABC):
    """
    Abstract base class for execution handlers.
    """
    @abstractmethod
    def execute_order(self, event: OrderEvent) -> None:
        pass

    @abstractmethod
    def process_market_event(self, event: MarketEvent) -> None:
        pass


class SimulatedExecutionHandler(AbstractExecutionHandler):
    """
    Simulated Execution Handler modeling real-world trade frictions.

    Incorporates the Almgren-Chriss Market Impact Model and optionally
    the Square-Root Impact Law.

    Mathematical Models:
    Almgren-Chriss Permanent Impact:
        $ \\Delta P_{perm} = \\theta \\cdot Q $
        where $\\theta$ is the permanent impact coefficient and $Q$ is trade size.

    Almgren-Chriss Temporary Impact:
        $ \\Delta P_{temp} = \\eta \\cdot \\frac{Q}{\\Delta t} $
        where $\\eta$ is the temporary impact coefficient and $\\Delta t$ is interval length.

    Square-Root Impact Law:
        $ \\Delta P_{sqrt} = Y \\cdot \\sigma \\cdot \\sqrt{\\frac{Q}{V}} $
        where $Y$ is a scaling factor, $\\sigma$ is volatility, $Q$ is trade size,
        and $V$ is the market volume.
    """
    def __init__(self,
                 queue: EventQueue,
                 data_handler: AbstractDataHandler,
                 max_volume_participation: float = 0.1,
                 commission_rate: float = 0.001,
                 use_square_root_impact: bool = False,
                 theta: float = 1e-6,
                 eta: float = 1e-6,
                 Y: float = 0.1,
                 dt: float = 1.0):
        self.queue = queue
        self.data_handler = data_handler
        self.max_volume_participation = max_volume_participation
        self.commission_rate = commission_rate

        # Impact model parameters
        self.use_square_root_impact = use_square_root_impact
        self.theta = theta
        self.eta = eta
        self.Y = Y
        self.dt = dt

        # Order queues for pending orders
        self.market_orders: Dict[str, List[OrderEvent]] = {}
        self.limit_orders: Dict[str, List[OrderEvent]] = {}
        self.stop_orders: Dict[str, List[OrderEvent]] = {}

    def execute_order(self, event: OrderEvent) -> None:
        """
        Receives an OrderEvent and either executes it immediately (if MARKET and data is available)
        or queues it for evaluation when the next market event arrives.
        """
        if event.order_type == OrderType.MARKET:
            bar = self.data_handler.get_latest_bar(event.symbol)
            if bar:
                self._execute_market_order(event, bar)
            else:
                if event.symbol not in self.market_orders:
                    self.market_orders[event.symbol] = []
                self.market_orders[event.symbol].append(event)
        elif event.order_type == OrderType.LIMIT:
            if event.symbol not in self.limit_orders:
                self.limit_orders[event.symbol] = []
            self.limit_orders[event.symbol].append(event)
        elif event.order_type == OrderType.STOP:
            if event.symbol not in self.stop_orders:
                self.stop_orders[event.symbol] = []
            self.stop_orders[event.symbol].append(event)

    def process_market_event(self, event: MarketEvent) -> None:
        """
        Processes new market data to evaluate pending market, limit, and stop orders.
        """
        symbol = event.symbol

        # Process Market Orders waiting for a bar
        if symbol in self.market_orders:
            orders = self.market_orders.pop(symbol, [])
            for order in orders:
                self._execute_market_order(order, event)

        # Process Stop Orders
        if symbol in self.stop_orders:
            orders = self.stop_orders.pop(symbol, [])
            active_stops = []
            for order in orders:
                triggered = False
                if order.direction == OrderDirection.BUY:
                    if event.high >= order.stop_price:
                        triggered = True
                else: # SELL
                    if event.low <= order.stop_price:
                        triggered = True

                if triggered:
                    # Convert to market order and execute
                    market_order = OrderEvent(
                        timestamp=event.timestamp,
                        symbol=order.symbol,
                        order_type=OrderType.MARKET,
                        direction=order.direction,
                        quantity=order.quantity,
                        order_id=order.order_id,
                        time_in_force=order.time_in_force
                    )
                    self._execute_market_order(market_order, event)
                else:
                    active_stops.append(order)
            if active_stops:
                if symbol not in self.stop_orders:
                    self.stop_orders[symbol] = []
                self.stop_orders[symbol].extend(active_stops)

        # Process Limit Orders
        if symbol in self.limit_orders:
            orders = self.limit_orders.pop(symbol, [])
            active_limits = []
            for order in orders:
                filled = False
                if order.direction == OrderDirection.BUY:
                    if event.low <= order.limit_price:
                        filled = True
                else: # SELL
                    if event.high >= order.limit_price:
                        filled = True

                if filled:
                    self._execute_limit_order(order, event)
                else:
                    active_limits.append(order)
            if active_limits:
                if symbol not in self.limit_orders:
                    self.limit_orders[symbol] = []
                self.limit_orders[symbol].extend(active_limits)

    def _execute_market_order(self, order: OrderEvent, current_bar: MarketEvent) -> None:
        if current_bar.volume <= 0:
            # wait for next bar
            if order.symbol not in self.market_orders:
                self.market_orders[order.symbol] = []
            self.market_orders[order.symbol].append(order)
            return

        max_qty = int(current_bar.volume * self.max_volume_participation)
        fill_qty = min(order.quantity, max_qty)

        if fill_qty <= 0:
            if order.symbol not in self.market_orders:
                self.market_orders[order.symbol] = []
            self.market_orders[order.symbol].append(order)
            return

        base_price = current_bar.close
        spread_cost = current_bar.bid_ask_spread / 2.0

        # Calculate impact
        impact = self._calculate_impact(fill_qty, current_bar)

        if order.direction == OrderDirection.BUY:
            fill_price = base_price + spread_cost + impact
        else:
            fill_price = base_price - spread_cost - impact

        commission = fill_price * fill_qty * self.commission_rate

        fill_event = FillEvent(
            timestamp=current_bar.timestamp,
            symbol=order.symbol,
            exchange="SIM",
            quantity_filled=fill_qty,
            fill_price=float(fill_price),
            direction=order.direction,
            commission_fee=float(commission),
            slippage_cost=float(spread_cost * fill_qty),
            impact_cost=float(impact * fill_qty),
            order_id=order.order_id
        )
        self.queue.put(fill_event)

        # Partial fill logic: queue the remainder for the next bar
        if fill_qty < order.quantity:
            remaining_qty = order.quantity - fill_qty
            new_order = OrderEvent(
                timestamp=current_bar.timestamp,
                symbol=order.symbol,
                order_type=order.order_type,
                direction=order.direction,
                quantity=remaining_qty,
                order_id=f"{order.order_id}-P",
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                time_in_force=order.time_in_force
            )
            if new_order.symbol not in self.market_orders:
                self.market_orders[new_order.symbol] = []
            self.market_orders[new_order.symbol].append(new_order)

    def _execute_limit_order(self, order: OrderEvent, current_bar: MarketEvent) -> None:
        if current_bar.volume <= 0:
            if order.symbol not in self.limit_orders:
                self.limit_orders[order.symbol] = []
            self.limit_orders[order.symbol].append(order)
            return

        max_qty = int(current_bar.volume * self.max_volume_participation)
        fill_qty = min(order.quantity, max_qty)

        if fill_qty <= 0:
            if order.symbol not in self.limit_orders:
                self.limit_orders[order.symbol] = []
            self.limit_orders[order.symbol].append(order)
            return

        # For limit orders, execution is at limit price
        fill_price = order.limit_price
        commission = fill_price * fill_qty * self.commission_rate

        fill_event = FillEvent(
            timestamp=current_bar.timestamp,
            symbol=order.symbol,
            exchange="SIM",
            quantity_filled=fill_qty,
            fill_price=float(fill_price),
            direction=order.direction,
            commission_fee=float(commission),
            slippage_cost=0.0,
            impact_cost=0.0,
            order_id=order.order_id
        )
        self.queue.put(fill_event)

        if fill_qty < order.quantity:
            remaining_qty = order.quantity - fill_qty
            new_order = OrderEvent(
                timestamp=current_bar.timestamp,
                symbol=order.symbol,
                order_type=order.order_type,
                direction=order.direction,
                quantity=remaining_qty,
                order_id=f"{order.order_id}-P",
                limit_price=order.limit_price,
                stop_price=order.stop_price,
                time_in_force=order.time_in_force
            )
            # Re-queue the remaining quantity as a limit order
            if new_order.symbol not in self.limit_orders:
                self.limit_orders[new_order.symbol] = []
            self.limit_orders[new_order.symbol].append(new_order)

    def _calculate_impact(self, quantity: int, bar: MarketEvent) -> float:
        if quantity <= 0:
            return 0.0

        if self.use_square_root_impact:
            # Estimate volatility sigma from High/Low
            sigma = (bar.high - bar.low) / bar.close if bar.close > 0 else 0.01
            impact = self.Y * sigma * math.sqrt(quantity / bar.volume)
        else:
            perm_impact = self.theta * quantity
            temp_impact = self.eta * (quantity / self.dt)
            impact = perm_impact + temp_impact

        return impact
