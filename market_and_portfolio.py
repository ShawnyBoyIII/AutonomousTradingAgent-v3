import os
import pandas as pd
import numpy as np
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple, Any, Generator
from dataclasses import dataclass, field
from decimal import Decimal

from event_infrastructure import (
    EventQueue, Event, MarketEvent, SignalEvent, OrderEvent, FillEvent,
    BarType, SignalDirection, OrderType, OrderDirection, TimeInForce,
    EventEngineError
)

class InsufficientCapitalError(EventEngineError):
    pass

class DataHandlerError(EventEngineError):
    pass

class PointInTimeLeakError(DataHandlerError):
    pass


class AbstractDataHandler(ABC):
    @abstractmethod
    def get_latest_bar(self, symbol: str) -> Optional[MarketEvent]:
        pass

    @abstractmethod
    def get_latest_price(self, symbol: str) -> float:
        pass

    @abstractmethod
    def update_bars(self) -> bool:
        """Pushes the next bar(s) to the queue. Returns True if successful, False if no more data."""
        pass


class HistoricCSVDataHandler(AbstractDataHandler):
    """
    Historic CSV/Parquet Data Handler.
    Merges and synchronizes multi-asset feeds chronologically into a single generator stream
    that pushes MarketEvent objects to the EventQueue.
    Enforces point-in-time lookup to prevent future data leakage.
    """
    def __init__(self, queue: EventQueue, file_paths: Dict[str, str]):
        """
        file_paths: dict mapping symbol to csv/parquet file path
        """
        self.queue = queue
        self.symbol_data: Dict[str, pd.DataFrame] = {}
        self.latest_symbol_data: Dict[str, List[MarketEvent]] = {}
        self.current_time_ns: int = 0

        self._load_and_merge_data(file_paths)

    def _load_and_merge_data(self, file_paths: Dict[str, str]):
        dfs = []
        for symbol, path in file_paths.items():
            self.latest_symbol_data[symbol] = []

            if path.endswith('.csv'):
                df = pd.read_csv(path)
            elif path.endswith('.parquet'):
                df = pd.read_parquet(path)
            else:
                raise ValueError(f"Unsupported file format for {path}")

            # Expecting columns: timestamp_ns, open, high, low, close, volume, bid_ask_spread
            df['symbol'] = symbol
            dfs.append(df)

        if dfs:
            combined = pd.concat(dfs, ignore_index=True)
            combined.sort_values('timestamp_ns', inplace=True)
            self.data_generator = combined.itertuples(index=False)
        else:
            self.data_generator = iter([])

    def update_bars(self) -> bool:
        try:
            row = next(self.data_generator)

            event = MarketEvent(
                timestamp=int(row.timestamp_ns),
                symbol=row.symbol,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                bid_ask_spread=float(row.bid_ask_spread),
                bar_type=BarType.BAR_1M # using 1M as a default since it's not always in data
            )

            self.current_time_ns = event.timestamp
            self.latest_symbol_data[row.symbol].append(event)
            self.queue.put(event)
            return True

        except StopIteration:
            return False

    def get_latest_bar(self, symbol: str) -> Optional[MarketEvent]:
        # Point-in-time lookup: only returns data that has been "pushed" to the engine.
        if symbol in self.latest_symbol_data and self.latest_symbol_data[symbol]:
            return self.latest_symbol_data[symbol][-1]
        return None

    def get_latest_price(self, symbol: str) -> float:
        bar = self.get_latest_bar(symbol)
        if bar:
            return bar.close
        return 0.0


@dataclass
class Position:
    symbol: str
    quantity: int = 0
    avg_price: Decimal = Decimal('0')

class Portfolio:
    """
    Portfolio accounting engine.
    Tracks holdings, cash, PnL, margin, and translates Signals to Orders.
    """
    def __init__(self, data_handler: AbstractDataHandler, queue: EventQueue,
                 initial_cash: float = 100000.0, max_leverage: float = 1.0,
                 max_concentration: float = 0.2, short_borrow_rate_annual: float = 0.05):
        self.data_handler = data_handler
        self.queue = queue
        self.initial_cash = Decimal(str(initial_cash))
        self.cash = self.initial_cash
        self.max_leverage = Decimal(str(max_leverage))
        self.max_concentration = Decimal(str(max_concentration))
        self.short_borrow_rate_annual = Decimal(str(short_borrow_rate_annual))

        self.positions: Dict[str, Position] = {}
        self.realized_pnl = Decimal('0')
        self.order_id_counter = 1
        self.last_timestamp_ns: Optional[int] = None

    def _generate_order_id(self) -> str:
        oid = f"ORD-{self.order_id_counter}"
        self.order_id_counter += 1
        return oid

    def get_position_quantity(self, symbol: str) -> int:
        return self.positions[symbol].quantity if symbol in self.positions else 0

    def update_time(self, current_timestamp_ns: int):
        """Called periodically (e.g. on every MarketEvent) to accrue time-based fees like short borrows."""
        if self.last_timestamp_ns is None:
            self.last_timestamp_ns = current_timestamp_ns
            return

        time_diff_ns = current_timestamp_ns - self.last_timestamp_ns
        if time_diff_ns <= 0:
            return

        # Nanoseconds in a 365-day year
        ns_in_year = Decimal('31536000000000000')
        time_fraction = Decimal(time_diff_ns) / ns_in_year

        for sym, pos in self.positions.items():
            if pos.quantity < 0:
                price = Decimal(str(self.data_handler.get_latest_price(sym) or pos.avg_price))
                short_value = abs(pos.quantity) * price
                fee = short_value * self.short_borrow_rate_annual * time_fraction
                self.cash -= fee

        self.last_timestamp_ns = current_timestamp_ns

    def update_signal(self, event: SignalEvent):
        """Translates a SignalEvent into an OrderEvent if risk limits allow."""
        current_qty = self.get_position_quantity(event.symbol)
        target_qty = event.target_quantity

        if event.signal_type == SignalDirection.EXIT:
            target_qty = 0
        elif event.signal_type == SignalDirection.SHORT:
            target_qty = -abs(target_qty)
        else: # LONG
            target_qty = abs(target_qty)

        qty_diff = target_qty - current_qty

        if qty_diff == 0:
            return

        current_price_float = self.data_handler.get_latest_price(event.symbol)
        if current_price_float <= 0:
            return # No market data to value the asset

        current_price = Decimal(str(current_price_float))
        direction = OrderDirection.BUY if qty_diff > 0 else OrderDirection.SELL

        order_event = OrderEvent(
            timestamp=event.timestamp,
            symbol=event.symbol,
            order_type=OrderType.MARKET,
            direction=direction,
            quantity=abs(qty_diff),
            order_id=self._generate_order_id(),
            time_in_force=TimeInForce.GTC
        )

        if not self._check_order_validity(order_event, current_price):
            raise InsufficientCapitalError(
                f"Order for {event.symbol} violates capital, leverage, or concentration limits."
            )

        self.queue.put(order_event)

    def _check_order_validity(self, order: OrderEvent, price: Decimal) -> bool:
        symbol = order.symbol
        qty_change = order.quantity if order.direction == OrderDirection.BUY else -order.quantity

        current_qty = self.get_position_quantity(symbol)
        new_qty = current_qty + qty_change

        current_equity = self.get_total_equity()
        if current_equity <= 0:
            return False

        total_gross_exposure = Decimal('0')
        for sym, pos in self.positions.items():
            sym_price = Decimal(str(self.data_handler.get_latest_price(sym) or 0))
            if sym == symbol:
                total_gross_exposure += abs(new_qty) * price
            else:
                total_gross_exposure += abs(pos.quantity) * sym_price

        if total_gross_exposure > current_equity * self.max_leverage:
            return False

        new_pos_exposure = abs(new_qty) * price
        if new_pos_exposure > current_equity * self.max_concentration:
            return False

        return True

    def get_total_equity(self) -> Decimal:
        """Cash + Unrealized PnL (equivalently: cash + net liquidation value of positions)."""
        equity = self.cash
        for sym, pos in self.positions.items():
            if pos.quantity != 0:
                price = Decimal(str(self.data_handler.get_latest_price(sym) or pos.avg_price))
                equity += pos.quantity * price
        return equity

    def get_unrealized_pnl(self) -> Decimal:
        unrealized = Decimal('0')
        for sym, pos in self.positions.items():
            if pos.quantity != 0:
                price = Decimal(str(self.data_handler.get_latest_price(sym) or pos.avg_price))
                unrealized += pos.quantity * (price - pos.avg_price)
        return unrealized

    def get_used_margin(self) -> Decimal:
        used = Decimal('0')
        for sym, pos in self.positions.items():
            if pos.quantity != 0:
                price = Decimal(str(self.data_handler.get_latest_price(sym) or pos.avg_price))
                used += abs(pos.quantity) * price
        return used

    def get_free_margin(self) -> Decimal:
        equity = self.get_total_equity()
        used = self.get_used_margin()
        return (equity * self.max_leverage) - used

    def update_fill(self, event: FillEvent):
        """Processes FillEvent to update portfolio balances and positions."""
        symbol = event.symbol
        fill_qty = event.quantity_filled
        if event.direction == OrderDirection.SELL:
            fill_qty = -fill_qty

        fill_price = Decimal(str(event.fill_price))
        commission = Decimal(str(event.commission_fee))
        slippage = Decimal(str(event.slippage_cost))
        impact = Decimal(str(event.impact_cost))

        total_costs = commission + slippage + impact

        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)

        pos = self.positions[symbol]
        current_qty = pos.quantity
        new_qty = current_qty + fill_qty

        cash_impact = (Decimal(fill_qty) * fill_price) + total_costs
        self.cash -= cash_impact

        if current_qty == 0 or (current_qty > 0 and fill_qty > 0) or (current_qty < 0 and fill_qty < 0):
            total_value = (abs(current_qty) * pos.avg_price) + (abs(fill_qty) * fill_price)
            pos.avg_price = total_value / abs(new_qty)
        else:
            if abs(fill_qty) <= abs(current_qty):
                if current_qty > 0:
                    realized = abs(fill_qty) * (fill_price - pos.avg_price)
                else:
                    realized = abs(fill_qty) * (pos.avg_price - fill_price)

                realized -= total_costs
                self.realized_pnl += realized
            else:
                closing_qty = abs(current_qty)
                if current_qty > 0:
                    realized = closing_qty * (fill_price - pos.avg_price)
                else:
                    realized = closing_qty * (pos.avg_price - fill_price)

                realized -= total_costs
                self.realized_pnl += realized
                pos.avg_price = fill_price

        pos.quantity = new_qty
        if pos.quantity == 0:
            pos.avg_price = Decimal('0')


# =============================================================================
# Unit Tests (pytest)
# =============================================================================

import pytest

def test_historic_data_handler(tmp_path):
    queue = EventQueue()

    df1 = pd.DataFrame({
        'timestamp_ns': [1000, 3000],
        'open': [10.0, 12.0],
        'high': [11.0, 13.0],
        'low': [9.0, 11.0],
        'close': [10.5, 12.5],
        'volume': [100, 200],
        'bid_ask_spread': [0.1, 0.1]
    })

    df2 = pd.DataFrame({
        'timestamp_ns': [2000, 4000],
        'open': [50.0, 52.0],
        'high': [51.0, 53.0],
        'low': [49.0, 51.0],
        'close': [50.5, 52.5],
        'volume': [1000, 2000],
        'bid_ask_spread': [0.5, 0.5]
    })

    file1 = tmp_path / "AAPL.csv"
    file2 = tmp_path / "TSLA.csv"

    df1.to_csv(file1, index=False)
    df2.to_csv(file2, index=False)

    handler = HistoricCSVDataHandler(queue, {"AAPL": str(file1), "TSLA": str(file2)})

    # Should push in order: AAPL(1000), TSLA(2000), AAPL(3000), TSLA(4000)
    assert handler.update_bars() == True
    assert handler.current_time_ns == 1000
    ev1 = queue.get()
    assert ev1.symbol == "AAPL"

    assert handler.update_bars() == True
    assert handler.current_time_ns == 2000
    ev2 = queue.get()
    assert ev2.symbol == "TSLA"

    assert handler.update_bars() == True
    assert handler.update_bars() == True
    assert handler.update_bars() == False

def test_portfolio_signal_to_order_and_fill(tmp_path):
    queue = EventQueue()
    df = pd.DataFrame({
        'timestamp_ns': [1000],
        'open': [100.0], 'high': [101.0], 'low': [99.0], 'close': [100.0],
        'volume': [1000], 'bid_ask_spread': [0.1]
    })
    file = tmp_path / "AAPL.csv"
    df.to_csv(file, index=False)

    handler = HistoricCSVDataHandler(queue, {"AAPL": str(file)})
    handler.update_bars()

    portfolio = Portfolio(
        data_handler=handler, queue=queue, initial_cash=10000.0,
        max_leverage=1.0, max_concentration=0.5
    )

    # Signal to buy 10 AAPL
    signal = SignalEvent(
        timestamp=2000, symbol="AAPL", signal_type=SignalDirection.LONG,
        strength=1.0, target_quantity=10
    )

    portfolio.update_signal(signal)

    # Check if OrderEvent was created
    order = queue.get() # first event is MarketEvent, then OrderEvent
    if isinstance(order, MarketEvent):
        order = queue.get()

    assert isinstance(order, OrderEvent)
    assert order.symbol == "AAPL"
    assert order.direction == OrderDirection.BUY
    assert order.quantity == 10

    # Process fill
    fill = FillEvent(
        timestamp=3000, symbol="AAPL", exchange="NASDAQ",
        quantity_filled=10, fill_price=100.0, direction=OrderDirection.BUY,
        commission_fee=1.0, slippage_cost=0.0, impact_cost=0.0, order_id=order.order_id
    )

    portfolio.update_fill(fill)

    assert portfolio.get_position_quantity("AAPL") == 10
    assert portfolio.cash == Decimal("9000.0") - Decimal("1.0") # 10000 - 1000 - 1 = 8999
    assert portfolio.get_total_equity() == Decimal("9999.0")

    # Check InsufficientCapitalError (Exceeds 50% concentration)
    signal2 = SignalEvent(
        timestamp=4000, symbol="AAPL", signal_type=SignalDirection.LONG,
        strength=1.0, target_quantity=100 # Total exposure 100 * 100 = 10000 > 5000 limit
    )
    with pytest.raises(InsufficientCapitalError):
        portfolio.update_signal(signal2)

def test_portfolio_short_and_borrow_fees(tmp_path):
    queue = EventQueue()
    df = pd.DataFrame({
        'timestamp_ns': [1000, 31536000000000000 + 1000], # + 1 year
        'open': [100.0, 100.0], 'high': [100.0, 100.0], 'low': [100.0, 100.0], 'close': [100.0, 100.0],
        'volume': [1000, 1000], 'bid_ask_spread': [0.1, 0.1]
    })
    file = tmp_path / "AAPL.csv"
    df.to_csv(file, index=False)

    handler = HistoricCSVDataHandler(queue, {"AAPL": str(file)})
    handler.update_bars() # ts=1000

    portfolio = Portfolio(
        data_handler=handler, queue=queue, initial_cash=10000.0,
        max_leverage=1.0, max_concentration=0.5, short_borrow_rate_annual=0.05
    )

    portfolio.update_time(1000)

    fill = FillEvent(
        timestamp=2000, symbol="AAPL", exchange="NASDAQ",
        quantity_filled=10, fill_price=100.0, direction=OrderDirection.SELL,
        commission_fee=0.0, slippage_cost=0.0, impact_cost=0.0, order_id="TEST"
    )

    portfolio.update_fill(fill)
    assert portfolio.get_position_quantity("AAPL") == -10
    assert portfolio.cash == Decimal("11000.0")

    # 1 year passes
    handler.update_bars() # ts=1 year + 1000
    portfolio.update_time(31536000000000000 + 1000)

    # Fee should be 1000 * 0.05 = 50
    # Cash should be 11000 - 50 = 10950
    assert portfolio.cash == Decimal("10950.0")

if __name__ == "__main__":
    pytest.main(["-v", __file__])
