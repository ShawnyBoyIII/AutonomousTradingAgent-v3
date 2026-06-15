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
        if fill_price <= 0:
            raise ValueError("fill price must be positive")

        gross = fill_price * order.quantity
        current_position = self.positions.get(order.ticker, 0)

        if order.side == "BUY":
            next_cash = self.cash - gross - self.fee_per_order
            if next_cash < 0:
                raise ValueError("insufficient cash for paper trade")

            next_position = current_position + order.quantity
        else:
            if current_position < order.quantity:
                raise ValueError("insufficient position for paper sell")

            next_cash = self.cash + gross - self.fee_per_order
            next_position = current_position - order.quantity

        fill = FillResult(
            order_id=str(uuid4()),
            ticker=order.ticker,
            quantity=order.quantity,
            fill_price=fill_price,
            fees=self.fee_per_order,
            filled_at=datetime.now(),
        )

        self.cash = next_cash
        self.positions[order.ticker] = next_position

        return fill
