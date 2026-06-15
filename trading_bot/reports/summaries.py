def build_daily_summary(
    realized_pnl: float,
    unrealized_pnl: float,
    open_positions: int,
) -> dict[str, float | int]:
    return {
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "open_positions": open_positions,
        "net_pnl": realized_pnl + unrealized_pnl,
    }
