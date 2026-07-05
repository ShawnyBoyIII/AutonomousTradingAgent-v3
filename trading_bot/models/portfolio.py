from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Position(BaseModel):
    ticker: str
    quantity: int = Field(ge=0)
    average_cost: float = Field(gt=0.0)
    stop_loss: float | None = Field(default=None, gt=0.0)
    profit_target: float | None = Field(default=None, gt=0.0)
    highest_high: float | None = Field(default=None, gt=0.0)
    initial_risk: float | None = Field(default=None, gt=0.0)
    entry_at: datetime | None = None
    strategy_tag: str = ""
    partial_profit_taken: bool = False


class PortfolioState(BaseModel):
    cash: float = Field(ge=0.0)
    equity: float = Field(ge=0.0)
    positions: dict[str, Position] = Field(default_factory=dict)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    last_exited_at: dict[str, str] = Field(default_factory=dict)
