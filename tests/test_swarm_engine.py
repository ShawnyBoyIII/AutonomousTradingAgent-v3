"""Tests for swarm execution engine."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

import pandas as pd
import pytest

from trading_bot.swarm.base import BaseSwarmWorker, WorkerConfig, WorkerResult, WorkerState
from trading_bot.swarm.engine import SwarmEngine
from trading_bot.swarm.results import CommitteeDecision, SwarmRunSummary
from trading_bot.swarm.workers import TechnicalAnalystWorker, WORKER_CLASSES


def _make_dataframe(n: int = 252, start_price: float = 100.0) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame."""
    import numpy as np
    dates = pd.date_range(end=datetime.now(), periods=n, freq="B")
    prices = start_price * np.exp(np.linspace(0.05, 0.15, n))
    highs = prices * 1.005
    lows = prices * 0.995
    opens = (highs + lows) / 2
    volumes = np.random.randint(100000, 1000000, n)
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    }, index=dates)


class TestSwarmEngineSetup:
    """SwarmEngine setup and initialization."""

    def test_engine_initializes_with_preset(self):
        engine = SwarmEngine(preset_name="investment_committee")
        assert engine.preset_name == "investment_committee"
        assert engine.max_concurrent == 3
        assert engine.workers == {}
        assert engine.results == {}
        assert engine.run_summary is None

    def test_engine_with_custom_max_concurrent(self):
        engine = SwarmEngine(preset_name="investment_committee", max_concurrent=5)
        assert engine.max_concurrent == 5

    def test_setup_workers(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        assert len(engine.workers) > 0
        for name, worker in engine.workers.items():
            assert worker.state == WorkerState.WAITING

    def test_setup_workers_missing_class_skipped(self):
        engine = SwarmEngine(preset_name="investment_committee")
        # Empty dict means no worker classes available
        engine.setup_workers({})
        assert len(engine.workers) == 0

    def test_setup_workers_resets_previous_workers(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        assert engine.workers

        engine.setup_workers({})

        assert engine.workers == {}
        assert engine.results == {}
        assert engine.run_summary is None

    def test_workers_have_correct_names(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        expected_names = {
            "technical_analyst",
            "fundamental_analyst",
            "sentiment_analyst",
            "risk_manager",
            "macro_strategist",
        }
        assert set(engine.workers.keys()) == expected_names


class TestSwarmEngineRun:
    """SwarmEngine execution."""

    def test_run_basic(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        assert isinstance(summary, SwarmRunSummary)
        assert summary.run_id
        assert summary.preset_name == "investment_committee"
        assert summary.symbols == ["AAPL"]
        assert summary.total_workers == len(engine.workers)
        assert summary.completed_workers > 0

    def test_run_returns_summary_with_decisions(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        assert len(summary.decisions) >= 1
        assert "AAPL" in summary.decisions
        decision = summary.decisions["AAPL"]
        assert isinstance(decision, CommitteeDecision)

    def test_run_with_multiple_symbols(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL", "SPY"], {"AAPL": df, "SPY": df})
        assert "AAPL" in summary.decisions
        assert "SPY" in summary.decisions

    def test_run_with_portfolio_state(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        portfolio_state = {
            "cash": 50000,
            "equity": 500000,
            "positions": {},
        }
        summary = engine.run(["AAPL"], {"AAPL": df}, portfolio_state)
        assert summary.completed_workers > 0

    def test_run_with_missing_market_data(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        summary = engine.run(["AAPL"], {})
        assert summary.completed_workers > 0

    def test_run_sets_completed_at(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        assert summary.completed_at is not None

    def test_run_sets_execution_time(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        assert summary.execution_time_seconds >= 0

    def test_run_stores_run_summary(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        engine.run(["AAPL"], {"AAPL": df})
        assert engine.run_summary is not None

    def test_run_with_custom_max_concurrent(self):
        engine = SwarmEngine(preset_name="investment_committee", max_concurrent=2)
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        assert summary.completed_workers > 0


class TestSwarmEngineDependencyResolution:
    """Worker dependency resolution."""

    def test_risk_manager_depends_on_technical_analyst(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        # Both workers start in WAITING state
        tech = engine.workers["technical_analyst"]
        risk = engine.workers["risk_manager"]
        assert tech.state == WorkerState.WAITING
        assert risk.state == WorkerState.WAITING
        assert "technical_analyst" in risk.config.depends_on

    def test_ready_workers_respects_dependencies(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        ready = engine._get_ready_workers()
        # technical_analyst has no dependencies, should be ready
        assert "technical_analyst" in ready
        # risk_manager depends on technical_analyst, but since both are waiting,
        # and technical_analyst is already DONE (state check), it should be ready
        # Actually, both are WAITING, so risk_manager is NOT ready yet
        # Let me check: _get_ready_workers checks if deps are DONE or FAILED
        # Since technical_analyst is WAITING, risk_manager should NOT be ready
        assert "risk_manager" not in ready

    def test_blocked_worker_when_dependency_failed(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        # Simulate technical_analyst failure
        tech = engine.workers["technical_analyst"]
        tech.state = WorkerState.FAILED
        ready = engine._get_ready_workers()
        # risk_manager should be blocked
        assert "risk_manager" not in ready
        assert engine.workers["risk_manager"].state == WorkerState.BLOCKED

    def test_dependent_worker_receives_prior_worker_results(self):
        class SourceWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                    data={"signal": "source-ready"},
                )

        class SinkWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                prior = kwargs.get("worker_results", {})
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                    data={"saw_source": prior["source"].data["signal"]},
                )

        engine = SwarmEngine(preset_name="investment_committee", max_concurrent=1)
        engine.workers = {
            "source": SourceWorker(WorkerConfig(name="source", preset="test")),
            "sink": SinkWorker(WorkerConfig(name="sink", preset="test", depends_on=["source"])),
        }

        engine.run(["AAPL"], {"AAPL": _make_dataframe()})

        assert engine.results["sink"].data["saw_source"] == "source-ready"

    def test_failed_dependency_blocks_dependent_worker(self):
        class FailingWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.FAILED,
                    error="boom",
                )

        class SinkWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                raise AssertionError("blocked dependency should not run")

        engine = SwarmEngine(preset_name="investment_committee", max_concurrent=1)
        engine.workers = {
            "source": FailingWorker(WorkerConfig(name="source", preset="test")),
            "sink": SinkWorker(WorkerConfig(name="sink", preset="test", depends_on=["source"])),
        }

        summary = engine.run(["AAPL"], {"AAPL": _make_dataframe()})

        assert engine.workers["source"].state == WorkerState.FAILED
        assert engine.workers["sink"].state == WorkerState.BLOCKED
        assert "sink" not in engine.results
        assert summary.failed_workers == 1
        assert summary.blocked_workers == 1

    def test_failed_worker_counts_as_abstain_in_committee_confidence(self):
        class BuyWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                    ticker_results={
                        "AAPL": {
                            "ticker": "AAPL",
                            "action": "BUY",
                            "confidence": 0.9,
                            "worker_name": self.config.name,
                            "preset": self.config.preset,
                            "reasons": ["buy"],
                            "metadata": {},
                        }
                    },
                )

        class FailingWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.FAILED,
                    error="boom",
                )

        engine = SwarmEngine(preset_name="investment_committee", max_concurrent=2)
        engine.workers = {
            "buyer": BuyWorker(WorkerConfig(name="buyer", preset="test")),
            "failing": FailingWorker(WorkerConfig(name="failing", preset="test")),
        }

        decision = engine.run(["AAPL"], {"AAPL": _make_dataframe()}).decisions["AAPL"]

        assert decision.votes_for == 1
        assert decision.votes_abstain == 1
        assert decision.total_workers == 2
        assert decision.confidence == 0.5

    def test_risk_manager_consumes_upstream_analyst_results(self):
        engine = SwarmEngine(preset_name="investment_committee", max_concurrent=1)
        engine.setup_workers(WORKER_CLASSES)

        engine.run(["AAPL"], {"AAPL": _make_dataframe()})

        tech_vote = engine.results["technical_analyst"].ticker_results["AAPL"]
        fund_vote = engine.results["fundamental_analyst"].ticker_results["AAPL"]
        risk_vote = engine.results["risk_manager"].ticker_results["AAPL"]
        assert risk_vote["metadata"]["technical_action"] == tech_vote["action"]
        assert risk_vote["metadata"]["technical_confidence"] == tech_vote["confidence"]
        assert risk_vote["metadata"]["fundamental_action"] == fund_vote["action"]
        assert risk_vote["metadata"]["fundamental_confidence"] == fund_vote["confidence"]
        decision = engine.run_summary.decisions["AAPL"]
        assert any("risk_manager handoff:" in risk for risk in decision.risk_factors)


class TestSwarmEngineAggregation:
    """Decision aggregation."""

    def test_aggregation_with_buy_signals(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        decision = summary.decisions["AAPL"]
        assert decision.ticker == "AAPL"
        assert decision.action in ["BUY", "SELL", "HOLD"]
        assert 0.0 <= decision.confidence <= 1.0

    def test_aggregation_carries_supporting_and_opposing_signals(self):
        class VoteWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                action = self.config.description
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                    ticker_results={
                        "AAPL": {
                            "ticker": "AAPL",
                            "action": action,
                            "confidence": 0.7,
                            "worker_name": self.config.name,
                            "preset": self.config.preset,
                            "reasons": [f"{action} reason"],
                            "metadata": {},
                        }
                    },
                )

        engine = SwarmEngine(preset_name="investment_committee")
        engine.workers = {
            "buyer": VoteWorker(WorkerConfig(name="buyer", preset="test", description="BUY")),
            "seller": VoteWorker(WorkerConfig(name="seller", preset="test", description="SELL")),
        }

        decision = engine.run(["AAPL"], {"AAPL": _make_dataframe()}).decisions["AAPL"]

        assert [vote.worker_name for vote in decision.supporting_signals] == ["buyer"]
        assert [vote.worker_name for vote in decision.opposing_signals] == ["seller"]

    def test_aggregation_counts_votes(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        decision = summary.decisions["AAPL"]
        total = decision.votes_for + decision.votes_against + decision.votes_abstain
        assert total == decision.total_workers

    def test_aggregation_uses_accuracy_weights(self):
        class VoteWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                action = self.config.description
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                    ticker_results={
                        "AAPL": {
                            "ticker": "AAPL",
                            "action": action,
                            "confidence": 0.7,
                            "worker_name": self.config.name,
                            "preset": self.config.preset,
                            "reasons": [f"{action} reason"],
                            "metadata": {},
                        }
                    },
                )

        engine = SwarmEngine(preset_name="investment_committee")
        engine.workers = {
            "light_seller": VoteWorker(WorkerConfig(name="light_seller", preset="test", description="SELL", accuracy_weight=0.8)),
            "heavy_buyer": VoteWorker(WorkerConfig(name="heavy_buyer", preset="test", description="BUY", accuracy_weight=1.2)),
        }

        decision = engine.run(["AAPL"], {"AAPL": _make_dataframe()}).decisions["AAPL"]

        assert decision.action == "BUY"
        assert decision.confidence == 0.6

    def test_aggregation_with_no_workers(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers({})
        summary = engine.run(["AAPL"], {"AAPL": _make_dataframe()})
        # No workers means no signals, so aggregation produces HOLD decision
        assert "AAPL" in summary.decisions
        decision = summary.decisions["AAPL"]
        assert decision.action == "HOLD"
        assert decision.confidence == 0.0

    def test_run_writes_worker_vote_log_when_path_provided(self, tmp_path: Path):
        class VoteWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                action = self.config.description
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                    ticker_results={
                        "AAPL": {
                            "ticker": "AAPL",
                            "action": action,
                            "confidence": 0.7,
                            "worker_name": self.config.name,
                            "preset": self.config.preset,
                            "reasons": [f"{action} reason"],
                            "metadata": {},
                        }
                    },
                )

        engine = SwarmEngine(preset_name="investment_committee")
        engine.workers = {
            "buyer": VoteWorker(WorkerConfig(name="buyer", preset="test", description="BUY", accuracy_weight=1.2)),
            "seller": VoteWorker(WorkerConfig(name="seller", preset="test", description="SELL", accuracy_weight=0.8)),
        }
        vote_log = tmp_path / "worker_votes.jsonl"

        engine.run(["AAPL"], {"AAPL": _make_dataframe()}, vote_log_path=vote_log)

        lines = vote_log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        payloads = [json.loads(line) for line in lines]
        assert payloads[0]["ticker"] == "AAPL"
        assert {payload["worker_name"] for payload in payloads} == {"buyer", "seller"}
        assert {payload["accuracy_weight"] for payload in payloads} == {1.2, 0.8}


class TestSwarmEngineEdgeCases:
    """Edge cases and error handling."""

    def test_run_with_empty_symbols(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run([], {"AAPL": df})
        assert summary.symbols == []
        assert len(summary.decisions) == 0

    def test_run_with_empty_market_data(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        summary = engine.run(["AAPL"], {})
        assert summary.completed_workers > 0

    def test_run_preserves_results(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        engine.run(["AAPL"], {"AAPL": df})
        assert len(engine.results) > 0
        for name, result in engine.results.items():
            assert result.worker_name == name

    def test_run_resets_previous_worker_state(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        first = engine.run(["AAPL"], {"AAPL": df})
        second = engine.run(["MSFT"], {"MSFT": df})

        assert "AAPL" in first.decisions
        assert "AAPL" not in second.decisions
        assert "MSFT" in second.decisions
        assert all(worker.state == WorkerState.DONE for worker in engine.workers.values())

    def test_run_summary_completed_at_is_datetime(self):
        engine = SwarmEngine(preset_name="investment_committee")
        engine.setup_workers(WORKER_CLASSES)
        df = _make_dataframe()
        summary = engine.run(["AAPL"], {"AAPL": df})
        assert isinstance(summary.completed_at, datetime)
