from datetime import datetime, timezone
from uuid import uuid4

from trading_bot.execution.fills import apply_slippage, effective_slippage_bps
from trading_bot.models.order import FillResult, OrderRequest


class PaperBroker:
    def __init__(
        self,
        starting_cash: float,
        fee_per_order: float,
        slippage_bps: int,
        dynamic_slippage_enabled: bool = False,
        dynamic_slippage_notional_bps_per_10k: float = 1.0,
        dynamic_slippage_low_price_boost_bps: float = 5.0,
        dynamic_slippage_max_extra_bps: float = 25.0,
    ) -> None:
        self.cash = starting_cash
        self.positions: dict[str, int] = {}
        self.position_costs: dict[str, float] = {}
        self.fee_per_order = fee_per_order
        self.slippage_bps = slippage_bps
        self.dynamic_slippage_enabled = dynamic_slippage_enabled
        self.dynamic_slippage_notional_bps_per_10k = dynamic_slippage_notional_bps_per_10k
        self.dynamic_slippage_low_price_boost_bps = dynamic_slippage_low_price_boost_bps
        self.dynamic_slippage_max_extra_bps = dynamic_slippage_max_extra_bps

    def estimate_fill_price(self, order: OrderRequest, market_price: float) -> float:
        slippage_bps = effective_slippage_bps(
            base_bps=self.slippage_bps,
            price=market_price,
            quantity=order.quantity,
            dynamic_enabled=self.dynamic_slippage_enabled,
            notional_bps_per_10k=self.dynamic_slippage_notional_bps_per_10k,
            low_price_boost_bps=self.dynamic_slippage_low_price_boost_bps,
            max_extra_bps=self.dynamic_slippage_max_extra_bps,
        )
        return apply_slippage(market_price, slippage_bps, order.side)

    def submit_order(self, order: OrderRequest, market_price: float) -> FillResult:
        fill_price = self.estimate_fill_price(order, market_price)
        if fill_price <= 0:
            raise ValueError("fill price must be positive")

        gross = fill_price * order.quantity
        current_position = self.positions.get(order.ticker, 0)
        current_cost = self.position_costs.get(order.ticker, 0.0)

        if order.side == "BUY":
            next_cash = self.cash - gross - self.fee_per_order
            if next_cash < 0:
                raise ValueError("insufficient cash for paper trade")

            next_position = current_position + order.quantity
            total_cost = current_cost * current_position + fill_price * order.quantity
            next_cost = total_cost / next_position if next_position > 0 else 0.0
        else:
            if current_position < order.quantity:
                raise ValueError("insufficient position for paper sell")

            next_cash = self.cash + gross - self.fee_per_order
            next_position = current_position - order.quantity
            next_cost = current_cost if next_position > 0 else 0.0

        fill = FillResult(
            order_id=str(uuid4()),
            ticker=order.ticker,
            quantity=order.quantity,
            fill_price=fill_price,
            fees=self.fee_per_order,
            filled_at=datetime.now(timezone.utc),
        )

        self.cash = next_cash
        self.positions[order.ticker] = next_position
        self.position_costs[order.ticker] = next_cost

        return fill
