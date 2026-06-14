from datetime import datetime
from uuid import uuid4

from trading_bot.execution.fills import apply_slippage
from trading_bot.models.order import FillResult, OrderRequest


class PaperBroker:
    def __init__(self, starting_cash: float, fee_per_order: float, slippage_bps: int) -> None:
        self.cash = starting_cash
        self.positions: dict[str, int] = {}
        self.fee_per_order = fee_per_order
        self.slippage_bps = slippage_bps

    def submit_order(self, order: OrderRequest, market_price: float) -> FillResult:
        fill_price = apply_slippage(market_price, self.slippage_bps, order.side)
        gross = fill_price * order.quantity

        if order.side == "BUY":
            self.cash -= gross + self.fee_per_order
            self.positions[order.ticker] = self.positions.get(order.ticker, 0) + order.quantity
        else:
            self.cash += gross - self.fee_per_order
            self.positions[order.ticker] = self.positions.get(order.ticker, 0) - order.quantity

        return FillResult(
            order_id=str(uuid4()),
            ticker=order.ticker,
            quantity=order.quantity,
            fill_price=fill_price,
            fees=self.fee_per_order,
            filled_at=datetime.now(),
        )
