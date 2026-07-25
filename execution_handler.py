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

# =============================================================================
# Unit Tests (pytest)
# =============================================================================

import pytest
from event_infrastructure import BarType
from market_and_portfolio import HistoricCSVDataHandler

class MockDataHandler(AbstractDataHandler):
    def __init__(self):
        self.bars = {}

    def get_latest_bar(self, symbol: str) -> Optional[MarketEvent]:
        return self.bars.get(symbol)

    def get_latest_price(self, symbol: str) -> float:
        bar = self.get_latest_bar(symbol)
        return bar.close if bar else 0.0

    def update_bars(self) -> bool:
        return False

def test_market_order_execution():
    queue = EventQueue()
    dh = MockDataHandler()
    bar = MarketEvent(
        timestamp=1000, symbol="AAPL", open=100.0, high=102.0, low=99.0, close=101.0,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    dh.bars["AAPL"] = bar

    executor = SimulatedExecutionHandler(queue, dh, max_volume_participation=0.1)

    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.MARKET, direction=OrderDirection.BUY,
        quantity=50, order_id="O1"
    )

    executor.execute_order(order)

    fill = queue.get()
    assert isinstance(fill, FillEvent)
    assert fill.quantity_filled == 50
    # Base price: 101.0
    # Spread cost: 0.1
    # Impact cost > 0
    assert fill.fill_price > 101.1

def test_market_order_partial_fill():
    queue = EventQueue()
    dh = MockDataHandler()
    bar = MarketEvent(
        timestamp=1000, symbol="AAPL", open=100.0, high=102.0, low=99.0, close=101.0,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    dh.bars["AAPL"] = bar

    executor = SimulatedExecutionHandler(queue, dh, max_volume_participation=0.1)

    # 0.1 * 1000 = 100 max per bar
    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.MARKET, direction=OrderDirection.BUY,
        quantity=150, order_id="O1"
    )

    executor.execute_order(order)

    fill = queue.get()
    assert isinstance(fill, FillEvent)
    assert fill.quantity_filled == 100

    # The remaining 50 should be queued for the next bar
    assert len(executor.market_orders["AAPL"]) == 1
    assert executor.market_orders["AAPL"][0].quantity == 50

def test_stop_order_execution():
    queue = EventQueue()
    dh = MockDataHandler()

    executor = SimulatedExecutionHandler(queue, dh, max_volume_participation=0.1)

    # Stop buy order at 105
    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.STOP, direction=OrderDirection.BUY,
        quantity=50, order_id="O1", stop_price=105.0
    )

    executor.execute_order(order)
    assert len(executor.stop_orders["AAPL"]) == 1

    # Market event that does NOT trigger the stop (high = 104)
    bar1 = MarketEvent(
        timestamp=1100, symbol="AAPL", open=100.0, high=104.0, low=99.0, close=101.0,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    executor.process_market_event(bar1)
    assert len(executor.stop_orders["AAPL"]) == 1

    # Market event that DOES trigger the stop (high = 106)
    bar2 = MarketEvent(
        timestamp=1200, symbol="AAPL", open=100.0, high=106.0, low=99.0, close=105.5,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    executor.process_market_event(bar2)

    assert len(executor.stop_orders.get("AAPL", [])) == 0
    fill = queue.get()
    assert isinstance(fill, FillEvent)
    assert fill.quantity_filled == 50

def test_limit_order_execution():
    queue = EventQueue()
    dh = MockDataHandler()

    executor = SimulatedExecutionHandler(queue, dh, max_volume_participation=0.1)

    # Limit buy order at 98
    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.LIMIT, direction=OrderDirection.BUY,
        quantity=50, order_id="O1", limit_price=98.0
    )

    executor.execute_order(order)
    assert len(executor.limit_orders["AAPL"]) == 1

    # Market event that does NOT fill the limit (low = 99)
    bar1 = MarketEvent(
        timestamp=1100, symbol="AAPL", open=100.0, high=104.0, low=99.0, close=101.0,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    executor.process_market_event(bar1)
    assert len(executor.limit_orders["AAPL"]) == 1

    # Market event that DOES fill the limit (low = 97)
    bar2 = MarketEvent(
        timestamp=1200, symbol="AAPL", open=100.0, high=106.0, low=97.0, close=105.5,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    executor.process_market_event(bar2)

    assert len(executor.limit_orders.get("AAPL", [])) == 0
    fill = queue.get()
    assert isinstance(fill, FillEvent)
    assert fill.quantity_filled == 50
    assert fill.fill_price == 98.0

if __name__ == "__main__":
    pytest.main(["-v", __file__])
