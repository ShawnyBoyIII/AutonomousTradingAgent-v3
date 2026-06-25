def compute_unrealized_pnl(quantity: int, average_cost: float, market_price: float) -> float:
    return (market_price - average_cost) * quantity


def compute_position_market_value(quantity: int, market_price: float) -> float:
    return quantity * market_price


def compute_exposure_ratio(market_value: float, equity: float) -> float:
    if equity <= 0:
        return 0.0
    return market_value / equity


def compute_portfolio_heat(
    positions: dict,
    latest_prices: dict[str, float],
    equity: float,
    heat_multiplier: float = 1.0,
) -> float:
    """Calculate portfolio heat as unrealized loss percentage of equity.

    Heat only counts losses (negative P&L), not gains. This represents
    the "pain" the portfolio is currently experiencing.

    Args:
        positions: Dict of ticker -> Position objects
        latest_prices: Dict of ticker -> current market price
        equity: Total account equity
        heat_multiplier: Scales the computed heat (1.0 = normal, >1.0 =
            conservative).  Used when data is stale or missing to bias
            towards fail-closed behaviour.

    Returns:
        Portfolio heat as a percentage (0.03 = 3%)

    Example:
        Two positions: AAPL +$500, TSLA -$800
        Unrealized loss = $800
        Heat = $800 / $100,000 = 0.8%
    """
    if equity <= 0:
        return 0.0

    total_loss = 0.0
    for ticker, position in positions.items():
        if position.quantity <= 0:
            continue
        last_price = latest_prices.get(ticker, position.average_cost)
        upl = compute_unrealized_pnl(
            position.quantity,
            position.average_cost,
            last_price,
        )
        # Only count losses (negative P&L)
        if upl < 0:
            total_loss += abs(upl)

    return total_loss * heat_multiplier / equity
