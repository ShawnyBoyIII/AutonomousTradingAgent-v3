from __future__ import annotations

from pydantic import BaseModel, Field


class RiskDecision(BaseModel):
    approved: bool
    reason: str
    position_size: int = Field(ge=0)
    dollar_risk: float = Field(ge=0.0)
    portfolio_exposure_warning: str | None = None
