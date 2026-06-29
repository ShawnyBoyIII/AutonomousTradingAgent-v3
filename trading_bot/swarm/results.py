"""Swarm result models for signal aggregation and committee decisions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class SignalVote(BaseModel):
    """A single signal vote from a worker."""

    ticker: str
    action: str  # BUY, SELL, HOLD, EXIT
    confidence: float = Field(ge=0.0, le=1.0)
    worker_name: str
    preset: str
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkerVerdict(BaseModel):
    """Final verdict from a worker after analysis."""

    worker_name: str
    preset: str
    overall_recommendation: str  # STRONG_BUY, BUY, NEUTRAL, SELL, STRONG_SELL
    confidence: float = Field(ge=0.0, le=1.0)
    key_findings: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    analysis_summary: str = ""
    signals: list[SignalVote] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommitteeDecision(BaseModel):
    """Final committee decision aggregating all worker votes."""

    decision: str  # APPROVE, REJECT, HOLD_FOR_MORE_INFO
    confidence: float = Field(ge=0.0, le=1.0)
    ticker: str
    action: str  # BUY, SELL, HOLD
    votes_for: int = 0
    votes_against: int = 0
    votes_abstain: int = 0
    total_workers: int = 0
    key_rationale: str = ""
    supporting_signals: list[SignalVote] = Field(default_factory=list)
    opposing_signals: list[SignalVote] = Field(default_factory=list)
    risk_factors: list[str] = Field(default_factory=list)
    recommended_position_size: float = 0.0
    recommended_stop_loss: float | None = None
    recommended_target: float | None = None
    worker_verdicts: list[WorkerVerdict] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SwarmRunSummary(BaseModel):
    """Summary of a complete swarm execution."""

    run_id: str
    preset_name: str
    symbols: list[str]
    total_workers: int
    completed_workers: int = 0
    failed_workers: int = 0
    blocked_workers: int = 0
    decisions: dict[str, CommitteeDecision] = Field(default_factory=dict)
    execution_time_seconds: float = 0.0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
