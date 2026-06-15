def compute_unrealized_pnl(quantity: int, average_cost: float, market_price: float) -> float:
    return (market_price - average_cost) * quantity
