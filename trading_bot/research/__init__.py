"""Research autopilot: hypothesis → signal → backtest → learn loop.

Manages a research pipeline where hypotheses are automatically generated,
tested via backtests, evaluated, and used to generate new hypotheses.
"""

from __future__ import annotations

from typing import Any

from .models import (
    ExperimentResult,
    Hypothesis,
    HypothesisStatus,
    ResearchCycle,
)
from .engine import ResearchEngine
from .store import ResearchStore

__all__ = [
    "Hypothesis",
    "HypothesisStatus",
    "ExperimentResult",
    "ResearchCycle",
    "ResearchStore",
    "ResearchEngine",
]
