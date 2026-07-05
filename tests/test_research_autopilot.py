"""Tests for research autopilot system."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.research.engine import ResearchEngine
from trading_bot.research.models import (
    ExperimentResult,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ResearchCycle,
)
from trading_bot.research.store import ResearchStore


class TestResearchStore:
    """Tests for ResearchStore."""

    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "research.db")
        return ResearchStore(db_path)

    def test_save_and_get_hypothesis(self, store):
        hypothesis = Hypothesis(
            title="Test hypothesis",
            description="A test",
            category=HypothesisCategory.FACTOR_TWEAK,
        )
        store.save_hypothesis(hypothesis)
        retrieved = store.get_hypothesis(hypothesis.id)
        assert retrieved is not None
        assert retrieved.title == "Test hypothesis"
        assert retrieved.category == HypothesisCategory.FACTOR_TWEAK

    def test_list_hypotheses(self, store):
        for i in range(5):
            h = Hypothesis(
                title=f"Hypothesis {i}",
                description=f"Description {i}",
            )
            store.save_hypothesis(h)

        all_h = store.list_hypotheses()
        assert len(all_h) == 5

        pending_h = store.list_hypotheses(status=HypothesisStatus.PENDING)
        assert len(pending_h) == 5

    def test_update_hypothesis_status(self, store):
        hypothesis = Hypothesis(
            title="Test",
            description="A test",
        )
        store.save_hypothesis(hypothesis)
        store.update_hypothesis_status(
            hypothesis.id, HypothesisStatus.RUNNING, "Running test"
        )
        retrieved = store.get_hypothesis(hypothesis.id)
        assert retrieved.status == HypothesisStatus.RUNNING
        assert retrieved.notes == "Running test"

    def test_save_experiment_result(self, store):
        hypothesis = Hypothesis(
            title="Test",
            description="A test",
        )
        store.save_hypothesis(hypothesis)

        result = ExperimentResult(
            hypothesis_id=hypothesis.id,
            backtest_start="2024-01-01",
            backtest_end="2025-06-01",
            symbols=["AAPL"],
            total_return=0.15,
            win_rate=0.55,
            sharpe_ratio=1.2,
            max_drawdown=0.08,
            total_trades=20,
            profit_factor=1.5,
        )
        row_id = store.save_experiment_result(result)
        assert row_id > 0

        results = store.get_experiment_results(hypothesis.id)
        assert len(results) == 1
        assert results[0].total_return == 0.15

    def test_save_and_get_cycle(self, store):
        hypothesis = Hypothesis(
            title="Test",
            description="A test",
        )
        store.save_hypothesis(hypothesis)

        result = ExperimentResult(
            hypothesis_id=hypothesis.id,
            backtest_start="2024-01-01",
            backtest_end="2025-06-01",
            symbols=["AAPL"],
            win_rate=0.50,
            sharpe_ratio=0.8,
        )
        store.save_experiment_result(result)

        cycle = ResearchCycle(
            hypothesis=hypothesis,
            experiment_result=result,
            evaluation="Good result",
        )
        store.save_cycle(cycle)

        cycles = store.list_cycles()
        assert len(cycles) == 1
        assert cycles[0].evaluation == "Good result"

    def test_get_stats(self, store):
        for i in range(3):
            h = Hypothesis(
                title=f"H{i}",
                description=f"Desc {i}",
            )
            store.save_hypothesis(h)

        # Mark one as passed
        passed_h = store.get_hypothesis(
            store.list_hypotheses()[0].id
        )
        store.update_hypothesis_status(
            passed_h.id, HypothesisStatus.PASSED, "Passed"
        )

        stats = store.get_stats()
        assert stats["total_hypotheses"] == 3
        assert stats["passed_count"] == 1
        assert stats["pending_count"] == 2


class TestResearchEngine:
    """Tests for ResearchEngine."""

    @pytest.fixture
    def engine(self, tmp_path):
        db_path = str(tmp_path / "research.db")
        store = ResearchStore(db_path)
        return ResearchEngine(store)

    def test_create_hypothesis(self, engine):
        h = engine.create_hypothesis(
            title="Test",
            description="A test",
            category=HypothesisCategory.FACTOR_TWEAK,
        )
        assert h.title == "Test"
        assert h.category == HypothesisCategory.FACTOR_TWEAK
        assert h.status == HypothesisStatus.PENDING

    def test_run_single_cycle(self, engine):
        hypothesis = engine.create_hypothesis(
            title="Test cycle",
            description="Test a backtest cycle",
            parameters={"symbols": ["AAPL"], "start_date": "2024-01-01", "end_date": "2025-06-01"},
        )

        def mock_backtest(hyp):
            return {
                "total_return": 0.15,
                "win_rate": 0.55,
                "sharpe_ratio": 1.2,
                "max_drawdown": 0.08,
                "total_trades": 20,
                "profit_factor": 1.5,
                "avg_trade_pnl": 100.0,
            }

        cycle = engine.run_cycle(hypothesis, mock_backtest)
        assert cycle.hypothesis is not None
        assert cycle.hypothesis.status == HypothesisStatus.PASSED
        assert cycle.experiment_result is not None
        assert cycle.experiment_result.win_rate == 0.55

    def test_run_pending_hypotheses(self, engine):
        for i in range(3):
            engine.create_hypothesis(
                title=f"Hypothesis {i}",
                description=f"Test {i}",
                parameters={"symbols": ["AAPL"], "start_date": "2024-01-01", "end_date": "2025-06-01"},
            )

        def mock_backtest(hyp):
            return {
                "total_return": 0.10,
                "win_rate": 0.50,
                "sharpe_ratio": 0.8,
                "max_drawdown": 0.10,
                "total_trades": 15,
                "profit_factor": 1.2,
                "avg_trade_pnl": 50.0,
            }

        cycles = engine.run_pending_hypotheses(mock_backtest, max_cycles=2)
        assert len(cycles) == 2

    def test_auto_generate_from_benching_alive(self, engine):
        benching_results = {
            "qlib": {
                "factors": [
                    {
                        "factor_name": "momentum_20d",
                        "categorization": "alive",
                        "ic_ir": 0.35,
                    },
                    {
                        "factor_name": "mean_reversion_5d",
                        "categorization": "reversed",
                        "ic_ir": -0.25,
                    },
                ]
            }
        }

        hypotheses = engine.auto_generate_hypotheses_from_benching(benching_results)
        assert len(hypotheses) == 2

        alive_h = [h for h in hypotheses if "momentum" in h.title]
        assert len(alive_h) == 1
        assert alive_h[0].category == HypothesisCategory.FACTOR_TWEAK

        reversed_h = [h for h in hypotheses if "Reverse" in h.title]
        assert len(reversed_h) == 1
        assert reversed_h[0].parameters.get("action") == "reverse_signal"

    def test_get_stats(self, engine):
        for i in range(5):
            engine.create_hypothesis(
                title=f"H{i}",
                description=f"Test {i}",
            )

        stats = engine.get_stats()
        assert stats["total_hypotheses"] == 5
        assert stats["pending_count"] == 5


class TestExperimentResult:
    """Tests for ExperimentResult model."""

    def test_is_successful(self):
        result = ExperimentResult(
            hypothesis_id="test_1",
            backtest_start="2024-01-01",
            backtest_end="2025-06-01",
            symbols=["AAPL"],
            win_rate=0.50,
            sharpe_ratio=1.0,
            max_drawdown=0.10,
        )
        assert result.is_successful() is True

    def test_is_unsuccessful_low_sharpe(self):
        result = ExperimentResult(
            hypothesis_id="test_2",
            backtest_start="2024-01-01",
            backtest_end="2025-06-01",
            symbols=["AAPL"],
            win_rate=0.50,
            sharpe_ratio=0.3,
            max_drawdown=0.10,
        )
        assert result.is_successful() is False

    def test_is_unsuccessful_high_drawdown(self):
        result = ExperimentResult(
            hypothesis_id="test_3",
            backtest_start="2024-01-01",
            backtest_end="2025-06-01",
            symbols=["AAPL"],
            win_rate=0.50,
            sharpe_ratio=1.0,
            max_drawdown=0.25,
        )
        assert result.is_successful() is False


class TestHypothesis:
    """Tests for Hypothesis model."""

    def test_mark_running(self):
        h = Hypothesis(title="Test", description="A test")
        h.mark_running()
        assert h.status == HypothesisStatus.RUNNING

    def test_mark_passed(self):
        h = Hypothesis(title="Test", description="A test")
        h.mark_passed("Good results")
        assert h.status == HypothesisStatus.PASSED
        assert h.notes == "Good results"

    def test_mark_failed(self):
        h = Hypothesis(title="Test", description="A test")
        h.mark_failed("Poor results")
        assert h.status == HypothesisStatus.FAILED
        assert h.notes == "Poor results"

    def test_mark_inconclusive(self):
        h = Hypothesis(title="Test", description="A test")
        h.mark_inconclusive("Mixed results")
        assert h.status == HypothesisStatus.INCONCLUSIVE
        assert h.notes == "Mixed results"


class TestResearchCycle:
    """Tests for ResearchCycle model."""

    def test_cycle_with_all_fields(self):
        hypothesis = Hypothesis(title="Test", description="A test")
        experiment = ExperimentResult(
            hypothesis_id="test_1",
            backtest_start="2024-01-01",
            backtest_end="2025-06-01",
            symbols=["AAPL"],
        )
        next_hyp = Hypothesis(title="Follow-up", description="Next step")

        cycle = ResearchCycle(
            hypothesis=hypothesis,
            experiment_result=experiment,
            evaluation="Good",
            next_hypothesis=next_hyp,
        )
        assert cycle.hypothesis.title == "Test"
        assert cycle.evaluation == "Good"
        assert cycle.next_hypothesis.title == "Follow-up"

    def test_cycle_with_none_fields(self):
        cycle = ResearchCycle()
        assert cycle.hypothesis is None
        assert cycle.experiment_result is None
        assert cycle.next_hypothesis is None
