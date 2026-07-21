from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ExperimentStatus = Literal[
    "PROPOSED",
    "OFFLINE_REJECTED",
    "CANARY",
    "KEPT",
    "ROLLED_BACK",
    "INCONCLUSIVE",
    "ERROR",
]


class ParameterChange(BaseModel):
    section: str
    field: str
    baseline: float
    candidate: float


class MetricSet(BaseModel):
    trades: int = 0
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0


class ExperimentState(BaseModel):
    experiment_id: str
    status: ExperimentStatus = "PROPOSED"
    change: ParameterChange
    started_at: datetime
    canary_closed_trades: int = 0
    market_sessions: list[str] = Field(default_factory=list)
    baseline_metrics: MetricSet | None = None
    candidate_metrics: MetricSet | None = None
    shadow_metrics: MetricSet | None = None
    last_error: str | None = None
    rolled_back_at: datetime | None = None
    candidate_checksum: str | None = None
    baseline_checksum: str | None = None
    baseline_was_absent: bool = False