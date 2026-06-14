from typing import Protocol

from trading_bot.models.order import FillResult, OrderRequest


class BrokerAdapter(Protocol):
    def submit_order(self, order: OrderRequest, market_price: float) -> FillResult: ...
