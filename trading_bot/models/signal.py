from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TradeSignal(BaseModel):
    ticker: str
    timeframe: Literal["daily", "intraday"]
    action: Literal["BUY", "SELL", "HOLD", "EXIT"]
    entry_price: float = Field(gt=0.0)
    stop_loss: float = Field(gt=0.0)
    profit_target: float = Field(gt=0.0)
    risk_reward_ratio: float = Field(gt=0.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)
    strategy_tag: str
    timestamp: datetime
