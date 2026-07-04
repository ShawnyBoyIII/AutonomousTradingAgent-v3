"""Research autopilot data models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HypothesisStatus(str, Enum):
    """Status of a hypothesis in the research pipeline."""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class HypothesisCategory(str, Enum):
    """Category of research hypothesis."""

    FACTOR_TWEAK = "factor_tweak"
    PARAMETER_OPTIMIZATION = "parameter_optimization"
    REGIME_DEPENDENT = "regime_dependent"
    CROSS_ASSET = "cross_asset"
    RISK_MANAGEMENT = "risk_management"
    ENTRY_EXIT = "entry_exit"
    POSITION_SIZING = "position_sizing"
    CUSTOM = "custom"


class Hypothesis(BaseModel):
    """A research hypothesis to test."""

    id: str = Field(default_factory=lambda: f"hyp_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}")
    title: str
    description: str
    category: HypothesisCategory = HypothesisCategory.CUSTOM
    status: HypothesisStatus = HypothesisStatus.PENDING
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    parent_hypothesis_id: str | None = None
    notes: str = ""

    def mark_running(self) -> None:
        """Mark hypothesis as running."""
        self.status = HypothesisStatus.RUNNING
        self.updated_at = datetime.now(timezone.utc)

    def mark_passed(self, result_summary: str) -> None:
        """Mark hypothesis as passed."""
        self.status = HypothesisStatus.PASSED
        self.notes = result_summary
        self.updated_at = datetime.now(timezone.utc)

    def mark_failed(self, result_summary: str) -> None:
        """Mark hypothesis as failed."""
        self.status = HypothesisStatus.FAILED
        self.notes = result_summary
        self.updated_at = datetime.now(timezone.utc)

    def mark_inconclusive(self, result_summary: str) -> None:
        """Mark hypothesis as inconclusive."""
        self.status = HypothesisStatus.INCONCLUSIVE
        self.notes = result_summary
        self.updated_at = datetime.now(timezone.utc)


class ExperimentResult(BaseModel):
    """Result from running a backtest experiment."""

    id: int | None = None
    hypothesis_id: str
    backtest_start: str
    backtest_end: str
    symbols: list[str]
    total_return: float = 0.0
    win_rate: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    total_trades: int = 0
    profit_factor: float = 0.0
    avg_trade_pnl: float = 0.0
    metrics: dict[str, Any] = Field(default_factory=dict)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def is_successful(self, min_win_rate: float = 0.45, min_sharpe: float = 0.5) -> bool:
        """Check if experiment meets success criteria."""
        return (
            self.win_rate >= min_win_rate
            and self.sharpe_ratio >= min_sharpe
            and self.max_drawdown < 0.20
        )


class ResearchCycle(BaseModel):
    """A complete research cycle: hypothesis → backtest → evaluation."""

    cycle_id: str = Field(default_factory=lambda: f"cycle_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}")
    hypothesis: Hypothesis | None = None
    experiment_result: ExperimentResult | None = None
    evaluation: str = ""
    next_hypothesis: Hypothesis | None = None
    completed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
