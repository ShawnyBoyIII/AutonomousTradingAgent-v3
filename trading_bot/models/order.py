from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class OrderRequest(BaseModel):
    ticker: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["market", "limit", "stop", "bracket"]
    quantity: int = Field(gt=0)
    submitted_at: datetime
    limit_price: float | None = Field(default=None, gt=0.0)
    stop_price: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def validate_order_prices(self) -> "OrderRequest":
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.order_type == "stop" and self.stop_price is None:
            raise ValueError("stop orders require stop_price")
        if self.order_type == "bracket":
            missing = []
            if self.limit_price is None:
                missing.append("limit_price")
            if self.stop_price is None:
                missing.append("stop_price")
            if missing:
                raise ValueError(f"bracket orders require {', '.join(missing)}")
        return self


class FillResult(BaseModel):
    order_id: str
    ticker: str
    quantity: int = Field(gt=0)
    fill_price: float = Field(gt=0.0)
    fees: float = Field(ge=0.0)
    filled_at: datetime
