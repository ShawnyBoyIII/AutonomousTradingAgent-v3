from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OrderRequest(BaseModel):
    ticker: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["market", "limit", "stop", "bracket"]
    quantity: int = Field(gt=0)
    submitted_at: datetime
    limit_price: float | None = None
    stop_price: float | None = None


class FillResult(BaseModel):
    order_id: str
    ticker: str
    quantity: int
    fill_price: float = Field(gt=0.0)
    fees: float = Field(ge=0.0)
    filled_at: datetime
