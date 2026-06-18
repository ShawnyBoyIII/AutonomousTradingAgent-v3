from __future__ import annotations

from pydantic import BaseModel, Field


class Position(BaseModel):
    ticker: str
    quantity: int = Field(ge=0)
    average_cost: float = Field(gt=0.0)
    stop_loss: float | None = Field(default=None, gt=0.0)
    profit_target: float | None = Field(default=None, gt=0.0)


class PortfolioState(BaseModel):
    cash: float = Field(ge=0.0)
    equity: float = Field(ge=0.0)
    positions: dict[str, Position] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
