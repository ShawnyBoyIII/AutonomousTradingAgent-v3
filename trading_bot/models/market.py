from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class MarketBar(BaseModel):
    ticker: str
    timeframe: Literal["daily", "intraday"]
    timestamp: datetime
    open: float = Field(gt=0.0)
    high: float = Field(gt=0.0)
    low: float = Field(gt=0.0)
    close: float = Field(gt=0.0)
    volume: int = Field(ge=0)


class MarketSnapshot(BaseModel):
    ticker: str
    timestamp: datetime
    last_price: float = Field(gt=0.0)
    daily_change_pct: float | None = None
    volume: int | None = Field(default=None, ge=0)


class MarketScanResult(BaseModel):
    ticker: str
    timeframe: Literal["daily", "intraday"]
    score: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
