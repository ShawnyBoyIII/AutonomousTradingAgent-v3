from __future__ import annotations

from typing import Any


def diagnostics(
    *,
    trades: int,
    wins: int,
    losses: int,
    net_pnl: float,
    gross_profit: float = 0.0,
    gross_loss: float = 0.0,
) -> dict[str, float]:
    loss_abs = abs(gross_loss)
    return {
        "avg_win": round(gross_profit / wins, 2) if wins else 0.0,
        "avg_loss": round(gross_loss / losses, 2) if losses else 0.0,
        "expectancy": round(net_pnl / trades, 2) if trades else 0.0,
        "pnl_per_trade": round(net_pnl / trades, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / loss_abs, 2)
        if loss_abs
        else (round(gross_profit, 2) if gross_profit else 0.0),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
    }


def attach_diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    result.update(
        diagnostics(
            trades=int(result.get("trades", 0) or 0),
            wins=int(result.get("wins", 0) or 0),
            losses=int(result.get("losses", 0) or 0),
            net_pnl=float(result.get("net_pnl", 0.0) or 0.0),
            gross_profit=float(result.get("gross_profit", 0.0) or 0.0),
            gross_loss=float(result.get("gross_loss", 0.0) or 0.0),
        )
    )
    return result
