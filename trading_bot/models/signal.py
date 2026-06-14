from __future__ import annotations

from math import isclose
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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

    @model_validator(mode="after")
    def validate_trade_setup(self) -> "TradeSignal":
        if self.action == "BUY":
            if not (self.stop_loss < self.entry_price < self.profit_target):
                raise ValueError("BUY trade requires stop_loss < entry_price < profit_target")

            expected_ratio = (self.profit_target - self.entry_price) / (
                self.entry_price - self.stop_loss
            )
            if not isclose(self.risk_reward_ratio, expected_ratio, rel_tol=1e-6, abs_tol=1e-9):
                raise ValueError("risk_reward_ratio must match BUY price geometry")

        return self
