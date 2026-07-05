def apply_slippage(price: float, slippage_bps: float, side: str) -> float:
    direction = 1 if side == "BUY" else -1
    return price * (1 + (direction * slippage_bps / 10_000))


def effective_slippage_bps(
    *,
    base_bps: float,
    price: float,
    quantity: int,
    dynamic_enabled: bool = False,
    notional_bps_per_10k: float = 1.0,
    low_price_boost_bps: float = 5.0,
    max_extra_bps: float = 25.0,
) -> float:
    if not dynamic_enabled or quantity <= 0 or price <= 0:
        return float(base_bps)

    notional = price * quantity
    extra_bps = (notional / 10_000.0) * notional_bps_per_10k
    if price < 10.0:
        extra_bps += low_price_boost_bps
    extra_bps = min(extra_bps, max_extra_bps)
    return float(base_bps) + extra_bps
