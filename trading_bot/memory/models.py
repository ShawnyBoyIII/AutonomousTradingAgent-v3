"""Persistent memory data models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MemoryType(str, Enum):
    """Type of memory entry."""

    RESEARCH_FINDING = "research_finding"
    HYPOTHESIS_RESULT = "hypothesis_result"
    TRADING_INSIGHT = "trading_insight"
    PATTERN_RECOGNITION = "pattern_recognition"
    PARAMETER_TUNING = "parameter_tuning"
    RISK_OBSERVATION = "risk_observation"
    CUSTOM = "custom"


class MemoryEntry(BaseModel):
    """A single memory entry."""

    id: int | None = None
    memory_type: MemoryType = MemoryType.CUSTOM
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    relevance_score: float = 0.5
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    def update(self, content: str | None = None, tags: list[str] | None = None, relevance: float | None = None) -> None:
        """Update memory entry fields."""
        if content is not None:
            self.content = content
        if tags is not None:
            self.tags = tags
        if relevance is not None:
            self.relevance_score = relevance
        self.updated_at = datetime.now(timezone.utc)


class MemoryQuery(BaseModel):
    """Parameters for querying memories."""

    search_text: str = ""
    memory_type: MemoryType | None = None
    tags: list[str] = Field(default_factory=list)
    min_relevance: float = 0.0
    limit: int = 20
    sort_by: str = "relevance"  # relevance, date, custom


class MemoryStats(BaseModel):
    """Statistics about stored memories."""

    total_memories: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    recent_count_7d: int = 0
    recent_count_30d: int = 0
    avg_relevance: float = 0.0
    tag_count: int = 0
