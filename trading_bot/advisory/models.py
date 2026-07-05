from __future__ import annotations

from pydantic import BaseModel, Field


class AdvisoryObservation(BaseModel):
    ticker: str
    status: str
    reason: str = ""
    confidence: float = 0.0
    quality: str = ""
    entry_price: float | None = None
    supermodel_decision: str = ""
    swarm_decision: str = ""
    consensus: str = ""
    observed_at: str


class AdvisoryRecommendation(BaseModel):
    ticker: str
    score: float
    bucket: str
    observations: int = 0
    approval_rate: float = 0.0
    win_rate: float | None = None
    net_pnl: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    source_names: list[str] = Field(default_factory=list)


class AdvisoryRunSummary(BaseModel):
    observations_added: int = 0
    main_recommendations: int = 0
    cheap_recommendations: int = 0
    promoted_symbols: list[str] = Field(default_factory=list)
    avoided_symbols: list[str] = Field(default_factory=list)
