def compute_unrealized_pnl(quantity: int, average_cost: float, market_price: float) -> float:
    return (market_price - average_cost) * quantity


def compute_position_market_value(quantity: int, market_price: float) -> float:
    return quantity * market_price


def compute_exposure_ratio(market_value: float, equity: float) -> float:
    if equity <= 0:
        return 0.0
    return market_value / equity
