"""Tests for swarm result models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.swarm.results import (
    CommitteeDecision,
    SignalVote,
    SwarmRunSummary,
    WorkerVerdict,
)


class TestSignalVote:
    """SignalVote model validation."""

    def test_minimal_vote(self):
        vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.8,
            worker_name="trend_follower",
            preset="technical_analysis_panel",
        )
        assert vote.ticker == "AAPL"
        assert vote.action == "BUY"
        assert vote.confidence == 0.8
        assert vote.reasons == []
        assert vote.metadata == {}

    def test_vote_with_reasons(self):
        vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.8,
            worker_name="trend_follower",
            preset="technical_analysis_panel",
            reasons=["uptrend detected", "volume increasing"],
        )
        assert len(vote.reasons) == 2
        assert vote.reasons[0] == "uptrend detected"

    def test_vote_with_metadata(self):
        vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.8,
            worker_name="trend_follower",
            preset="technical_analysis_panel",
            metadata={"rsi": 65.2, "macd": 1.5},
        )
        assert vote.metadata["rsi"] == 65.2
        assert vote.metadata["macd"] == 1.5

    def test_confidence_boundary_zero(self):
        vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.0,
            worker_name="test",
            preset="default",
        )
        assert vote.confidence == 0.0

    def test_confidence_boundary_one(self):
        vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=1.0,
            worker_name="test",
            preset="default",
        )
        assert vote.confidence == 1.0

    def test_confidence_below_zero_raises(self):
        with pytest.raises(Exception):  # Pydantic validation error
            SignalVote(
                ticker="AAPL",
                action="BUY",
                confidence=-0.1,
                worker_name="test",
                preset="default",
            )

    def test_confidence_above_one_raises(self):
        with pytest.raises(Exception):
            SignalVote(
                ticker="AAPL",
                action="BUY",
                confidence=1.1,
                worker_name="test",
                preset="default",
            )

    def test_valid_actions(self):
        for action in ["BUY", "SELL", "HOLD", "EXIT"]:
            vote = SignalVote(
                ticker="AAPL",
                action=action,
                confidence=0.5,
                worker_name="test",
                preset="default",
            )
            assert vote.action == action

    def test_model_dump(self):
        vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.8,
            worker_name="trend_follower",
            preset="technical_analysis_panel",
        )
        data = vote.model_dump()
        assert data["ticker"] == "AAPL"
        assert data["action"] == "BUY"
        assert data["confidence"] == 0.8


class TestWorkerVerdict:
    """WorkerVerdict model validation."""

    def test_minimal_verdict(self):
        verdict = WorkerVerdict(
            worker_name="trend_follower",
            preset="technical_analysis_panel",
            overall_recommendation="BUY",
            confidence=0.7,
        )
        assert verdict.worker_name == "trend_follower"
        assert verdict.overall_recommendation == "BUY"
        assert verdict.confidence == 0.7
        assert verdict.key_findings == []
        assert verdict.risks == []
        assert verdict.analysis_summary == ""
        assert verdict.signals == []

    def test_verdict_with_findings(self):
        verdict = WorkerVerdict(
            worker_name="trend_follower",
            preset="technical_analysis_panel",
            overall_recommendation="BUY",
            confidence=0.7,
            key_findings=["price above EMA20", "RSI neutral"],
            risks=["low volume"],
            analysis_summary="Bullish trend detected",
        )
        assert len(verdict.key_findings) == 2
        assert len(verdict.risks) == 1
        assert verdict.analysis_summary == "Bullish trend detected"

    def test_verdict_with_signals(self):
        vote = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.8,
            worker_name="trend_follower",
            preset="technical_analysis_panel",
        )
        verdict = WorkerVerdict(
            worker_name="trend_follower",
            preset="technical_analysis_panel",
            overall_recommendation="BUY",
            confidence=0.7,
            signals=[vote],
        )
        assert len(verdict.signals) == 1
        assert verdict.signals[0].ticker == "AAPL"

    def test_all_recommendation_values(self):
        for rec in ["STRONG_BUY", "BUY", "NEUTRAL", "SELL", "STRONG_SELL"]:
            verdict = WorkerVerdict(
                worker_name="test",
                preset="default",
                overall_recommendation=rec,
                confidence=0.5,
            )
            assert verdict.overall_recommendation == rec

    def test_model_dump(self):
        verdict = WorkerVerdict(
            worker_name="trend_follower",
            preset="technical_analysis_panel",
            overall_recommendation="BUY",
            confidence=0.7,
        )
        data = verdict.model_dump()
        assert data["worker_name"] == "trend_follower"
        assert data["overall_recommendation"] == "BUY"


class TestCommitteeDecision:
    """CommitteeDecision model validation."""

    def test_minimal_decision(self):
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
        )
        assert decision.decision == "APPROVE"
        assert decision.confidence == 0.8
        assert decision.ticker == "AAPL"
        assert decision.action == "BUY"
        assert decision.votes_for == 0
        assert decision.votes_against == 0
        assert decision.votes_abstain == 0
        assert decision.total_workers == 0
        assert decision.key_rationale == ""
        assert decision.supporting_signals == []
        assert decision.opposing_signals == []
        assert decision.risk_factors == []
        assert decision.recommended_position_size == 0.0
        assert decision.recommended_stop_loss is None
        assert decision.recommended_target is None

    def test_decision_with_votes(self):
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
            votes_for=5,
            votes_against=1,
            votes_abstain=2,
            total_workers=8,
            key_rationale="Strong bullish signals from technical analysts",
        )
        assert decision.votes_for == 5
        assert decision.votes_against == 1
        assert decision.votes_abstain == 2
        assert decision.total_workers == 8
        assert decision.key_rationale == "Strong bullish signals from technical analysts"

    def test_decision_with_recommendations(self):
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
            recommended_position_size=0.15,
            recommended_stop_loss=145.0,
            recommended_target=175.0,
        )
        assert decision.recommended_position_size == 0.15
        assert decision.recommended_stop_loss == 145.0
        assert decision.recommended_target == 175.0

    def test_decision_with_risk_factors(self):
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
            risk_factors=["high volatility", "low liquidity"],
        )
        assert len(decision.risk_factors) == 2
        assert decision.risk_factors[0] == "high volatility"

    def test_decision_with_verdicts(self):
        verdict1 = WorkerVerdict(
            worker_name="trend_follower",
            preset="technical_analysis_panel",
            overall_recommendation="BUY",
            confidence=0.7,
        )
        verdict2 = WorkerVerdict(
            worker_name="mean_reversion",
            preset="technical_analysis_panel",
            overall_recommendation="NEUTRAL",
            confidence=0.5,
        )
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
            worker_verdicts=[verdict1, verdict2],
        )
        assert len(decision.worker_verdicts) == 2
        assert decision.worker_verdicts[0].worker_name == "trend_follower"

    def test_decision_with_supporting_and_opposing_signals(self):
        supporting = SignalVote(
            ticker="AAPL",
            action="BUY",
            confidence=0.9,
            worker_name="trend_follower",
            preset="technical_analysis_panel",
        )
        opposing = SignalVote(
            ticker="AAPL",
            action="SELL",
            confidence=0.6,
            worker_name="mean_reversion",
            preset="technical_analysis_panel",
        )
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
            supporting_signals=[supporting],
            opposing_signals=[opposing],
        )
        assert len(decision.supporting_signals) == 1
        assert len(decision.opposing_signals) == 1
        assert decision.supporting_signals[0].action == "BUY"
        assert decision.opposing_signals[0].action == "SELL"

    def test_decision_values(self):
        for decision_type in ["APPROVE", "REJECT", "HOLD_FOR_MORE_INFO"]:
            d = CommitteeDecision(
                decision=decision_type,
                confidence=0.5,
                ticker="AAPL",
                action="HOLD",
            )
            assert d.decision == decision_type

    def test_completed_at_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.5,
            ticker="AAPL",
            action="HOLD",
        )
        after = datetime.now(timezone.utc)
        assert before <= decision.completed_at <= after

    def test_model_dump(self):
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
        )
        data = decision.model_dump()
        assert data["decision"] == "APPROVE"
        assert data["ticker"] == "AAPL"
        assert data["action"] == "BUY"


class TestSwarmRunSummary:
    """SwarmRunSummary model validation."""

    def test_minimal_summary(self):
        summary = SwarmRunSummary(
            run_id="run-001",
            preset_name="investment_committee",
            symbols=["AAPL", "SPY"],
            total_workers=4,
        )
        assert summary.run_id == "run-001"
        assert summary.preset_name == "investment_committee"
        assert summary.symbols == ["AAPL", "SPY"]
        assert summary.total_workers == 4
        assert summary.completed_workers == 0
        assert summary.failed_workers == 0
        assert summary.blocked_workers == 0
        assert summary.decisions == {}
        assert summary.execution_time_seconds == 0.0

    def test_summary_with_completed_workers(self):
        summary = SwarmRunSummary(
            run_id="run-001",
            preset_name="investment_committee",
            symbols=["AAPL"],
            total_workers=4,
            completed_workers=3,
            failed_workers=1,
            blocked_workers=0,
            execution_time_seconds=12.5,
        )
        assert summary.completed_workers == 3
        assert summary.failed_workers == 1
        assert summary.blocked_workers == 0
        assert summary.execution_time_seconds == 12.5

    def test_summary_with_decisions(self):
        decision = CommitteeDecision(
            decision="APPROVE",
            confidence=0.8,
            ticker="AAPL",
            action="BUY",
        )
        summary = SwarmRunSummary(
            run_id="run-001",
            preset_name="investment_committee",
            symbols=["AAPL"],
            total_workers=4,
            decisions={"AAPL": decision},
        )
        assert summary.decisions["AAPL"] is decision

    def test_summary_with_metadata(self):
        summary = SwarmRunSummary(
            run_id="run-001",
            preset_name="investment_committee",
            symbols=["AAPL"],
            total_workers=4,
            metadata={"market_regime": "BULLISH", "volatility": "HIGH"},
        )
        assert summary.metadata["market_regime"] == "BULLISH"
        assert summary.metadata["volatility"] == "HIGH"

    def test_started_at_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        summary = SwarmRunSummary(
            run_id="run-001",
            preset_name="investment_committee",
            symbols=["AAPL"],
            total_workers=4,
        )
        after = datetime.now(timezone.utc)
        assert before <= summary.started_at <= after

    def test_completed_at_none_initially(self):
        summary = SwarmRunSummary(
            run_id="run-001",
            preset_name="investment_committee",
            symbols=["AAPL"],
            total_workers=4,
        )
        assert summary.completed_at is None

    def test_model_dump(self):
        summary = SwarmRunSummary(
            run_id="run-001",
            preset_name="investment_committee",
            symbols=["AAPL"],
            total_workers=4,
        )
        data = summary.model_dump()
        assert data["run_id"] == "run-001"
        assert data["preset_name"] == "investment_committee"
        assert data["symbols"] == ["AAPL"]
        assert data["total_workers"] == 4
