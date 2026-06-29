"""Base swarm worker and state management."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class WorkerState(str, Enum):
    """Worker execution states."""

    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"
    RETRYING = "retrying"


class WorkerConfig(BaseModel):
    """Configuration for a swarm worker."""

    name: str
    preset: str
    description: str = ""
    max_retries: int = 0
    timeout_seconds: int = 300
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 0


class WorkerResult(BaseModel):
    """Result from a single worker execution."""

    worker_name: str
    preset: str
    state: WorkerState
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    signals: list[dict[str, Any]] = Field(default_factory=list)
    analysis: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    ticker_results: dict[str, Any] = Field(default_factory=dict)


class BaseSwarmWorker(ABC):
    """Abstract base class for swarm workers.

    Each worker implements a specific analysis strategy (technical, fundamental,
    risk assessment, etc.) and produces signals or verdicts that feed into
    the committee decision process.
    """

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.state = WorkerState.WAITING
        self.result: WorkerResult | None = None
        self._started_at: datetime | None = None

    @property
    def is_ready(self) -> bool:
        """Check if worker dependencies are satisfied."""
        if self.state == WorkerState.BLOCKED:
            return False
        return True

    @abstractmethod
    def execute(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        """Execute the worker's analysis.

        Args:
            symbols: List of ticker symbols to analyze.
            market_data: Pre-fetched market data keyed by symbol.
            portfolio_state: Current portfolio state if available.
            **kwargs: Additional context data.

        Returns:
            WorkerResult with signals, analysis, and verdict.
        """
        ...

    def run(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> WorkerResult:
        """Run the worker with state management and retry logic.

        Args:
            symbols: List of ticker symbols to analyze.
            market_data: Pre-fetched market data keyed by symbol.
            portfolio_state: Current portfolio state if available.
            **kwargs: Additional context data.

        Returns:
            WorkerResult with execution outcome.
        """
        self.state = WorkerState.RUNNING
        self._started_at = datetime.now(timezone.utc)

        for attempt in range(self.config.max_retries + 1):
            try:
                self.state = WorkerState.RUNNING
                result = self.execute(
                    symbols=symbols,
                    market_data=market_data,
                    portfolio_state=portfolio_state,
                    **kwargs,
                )
                result.state = WorkerState.DONE
                result.started_at = self._started_at
                result.completed_at = datetime.now(timezone.utc)
                self.result = result
                self.state = WorkerState.DONE
                return result

            except Exception as e:
                error_msg = f"{type(e).__name__}: {e}"
                logger.warning(
                    "Worker %s attempt %d/%d failed: %s",
                    self.config.name,
                    attempt + 1,
                    self.config.max_retries + 1,
                    error_msg,
                )

                if attempt < self.config.max_retries:
                    self.state = WorkerState.RETRYING
                    continue

                self.state = WorkerState.FAILED
                result = WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.FAILED,
                    started_at=self._started_at,
                    completed_at=datetime.now(timezone.utc),
                    error=error_msg,
                )
                self.result = result
                return result

    def get_status(self) -> dict[str, Any]:
        """Get current worker status for streaming updates."""
        return {
            "name": self.config.name,
            "preset": self.config.preset,
            "state": self.state.value,
            "depends_on": self.config.depends_on,
            "error": self.result.error if self.result else None,
            "started_at": self._started_at.isoformat() if self._started_at else None,
        }

    def to_json(self) -> str:
        """Serialize worker result to JSON."""
        if self.result:
            return json.dumps(
                self.result.model_dump(),
                default=str,
                indent=2,
            )
        return json.dumps(self.get_status(), indent=2)
