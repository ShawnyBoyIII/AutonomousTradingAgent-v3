def apply_slippage(price: float, slippage_bps: int, side: str) -> float:
    direction = 1 if side == "BUY" else -1
    return price * (1 + (direction * slippage_bps / 10_000))
