"""Multi-agent swarm execution engine for trading strategy evaluation."""

from trading_bot.swarm.base import WorkerState, BaseSwarmWorker
from trading_bot.swarm.presets import (
    INVESTMENT_COMMITTEE,
    QUANT_DESK,
    RISK_COMMITTEE,
    TECHNICAL_ANALYSIS_PANEL,
    FUNDAMENTAL_ANALYSIS_TEAM,
    CRYPTO_DESK,
    MACRO_ECONOMICS_TEAM,
    ALL_PRESETS,
)
from trading_bot.swarm.engine import SwarmEngine
from trading_bot.swarm.results import (
    WorkerVerdict,
    CommitteeDecision,
    SignalVote,
)

__all__ = [
    "WorkerState",
    "BaseSwarmWorker",
    "INVESTMENT_COMMITTEE",
    "QUANT_DESK",
    "RISK_COMMITTEE",
    "TECHNICAL_ANALYSIS_PANEL",
    "FUNDAMENTAL_ANALYSIS_TEAM",
    "CRYPTO_DESK",
    "MACRO_ECONOMICS_TEAM",
    "ALL_PRESETS",
    "SwarmEngine",
    "WorkerVerdict",
    "CommitteeDecision",
    "SignalVote",
]
