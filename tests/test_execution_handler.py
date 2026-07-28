import pytest
from event_infrastructure import BarType, EventQueue, MarketEvent, OrderEvent, OrderType, OrderDirection, FillEvent
from market_and_portfolio import AbstractDataHandler, HistoricCSVDataHandler
from typing import Optional
from execution_handler import SimulatedExecutionHandler

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

def test_almgren_chriss_impact_model():
    queue = EventQueue()
    dh = MockDataHandler()
    bar = MarketEvent(
        timestamp=1000, symbol="AAPL", open=100.0, high=102.0, low=99.0, close=101.0,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    dh.bars["AAPL"] = bar

    # Set parameters for Almgren-Chriss
    theta = 1e-4
    eta = 1e-3
    dt = 1.0
    executor = SimulatedExecutionHandler(
        queue, dh, max_volume_participation=1.0,
        use_square_root_impact=False, theta=theta, eta=eta, dt=dt
    )

    quantity = 100
    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.MARKET, direction=OrderDirection.BUY,
        quantity=quantity, order_id="O_AC"
    )

    executor.execute_order(order)
    fill = queue.get()

    # Expected impact = (theta * Q) + (eta * Q / dt)
    # Expected impact = (1e-4 * 100) + (1e-3 * 100 / 1.0) = 0.01 + 0.1 = 0.11
    # Expected price = close(101.0) + spread(0.1) + impact(0.11) = 101.21
    assert abs(fill.fill_price - 101.21) < 1e-6

def test_square_root_impact_law():
    queue = EventQueue()
    dh = MockDataHandler()
    bar = MarketEvent(
        timestamp=1000, symbol="AAPL", open=100.0, high=103.0, low=99.0, close=101.0,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    dh.bars["AAPL"] = bar

    # Use square root impact law
    Y = 0.5
    executor = SimulatedExecutionHandler(
        queue, dh, max_volume_participation=1.0,
        use_square_root_impact=True, Y=Y
    )

    quantity = 100
    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.MARKET, direction=OrderDirection.BUY,
        quantity=quantity, order_id="O_SQRT"
    )

    executor.execute_order(order)
    fill = queue.get()

    # sigma = (high - low) / close = (103.0 - 99.0) / 101.0 = 4 / 101.0 ~ 0.03960396
    # impact = Y * sigma * sqrt(Q / V) = 0.5 * (4/101) * sqrt(100/1000)
    # sqrt(0.1) ~ 0.316227766
    # impact ~ 0.5 * 0.03960396 * 0.316227766 ~ 0.00626186

    sigma = (103.0 - 99.0) / 101.0
    import math
    expected_impact = Y * sigma * math.sqrt(100 / 1000.0)
    expected_price = 101.0 + 0.1 + expected_impact

    assert abs(fill.fill_price - expected_price) < 1e-6

def test_zero_volume_bar_execution():
    queue = EventQueue()
    dh = MockDataHandler()

    # Bar with zero volume
    bar = MarketEvent(
        timestamp=1000, symbol="AAPL", open=100.0, high=102.0, low=99.0, close=101.0,
        volume=0.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    dh.bars["AAPL"] = bar

    executor = SimulatedExecutionHandler(queue, dh, max_volume_participation=0.1)

    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.MARKET, direction=OrderDirection.BUY,
        quantity=50, order_id="O_ZERO_VOL"
    )

    executor.execute_order(order)

    # Nothing should be filled because volume is zero
    assert queue.empty()

    # The order should be queued
    assert len(executor.market_orders["AAPL"]) == 1
    assert executor.market_orders["AAPL"][0].quantity == 50

def test_limit_order_partial_fill():
    queue = EventQueue()
    dh = MockDataHandler()

    executor = SimulatedExecutionHandler(queue, dh, max_volume_participation=0.1)

    # Limit buy order at 98 for 200 quantity
    order = OrderEvent(
        timestamp=1000, symbol="AAPL", order_type=OrderType.LIMIT, direction=OrderDirection.BUY,
        quantity=200, order_id="O_LIMIT_PART", limit_price=98.0
    )

    executor.execute_order(order)
    assert len(executor.limit_orders["AAPL"]) == 1

    # Market event that DOES fill the limit (low = 97) but low volume
    bar = MarketEvent(
        timestamp=1200, symbol="AAPL", open=100.0, high=106.0, low=97.0, close=105.5,
        volume=1000.0, bid_ask_spread=0.2, bar_type=BarType.BAR_1M
    )
    # max_qty = 0.1 * 1000 = 100, which is < 200
    executor.process_market_event(bar)

    # First part should be filled
    fill = queue.get()
    assert fill.quantity_filled == 100
    assert fill.fill_price == 98.0

    # The remaining 100 should be re-queued
    assert len(executor.limit_orders["AAPL"]) == 1
    assert executor.limit_orders["AAPL"][0].quantity == 100
