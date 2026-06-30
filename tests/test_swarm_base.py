"""Tests for swarm base classes and worker lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading_bot.swarm.base import (
    BaseSwarmWorker,
    WorkerConfig,
    WorkerResult,
    WorkerState,
)


class TestWorkerConfig:
    """WorkerConfig model validation."""

    def test_default_values(self):
        config = WorkerConfig(name="test_worker", preset="default")
        assert config.description == ""
        assert config.max_retries == 0
        assert config.timeout_seconds == 300
        assert config.depends_on == []
        assert config.priority == 0

    def test_custom_values(self):
        config = WorkerConfig(
            name="test_worker",
            preset="default",
            description="A test worker",
            max_retries=3,
            timeout_seconds=60,
            depends_on=["dep1", "dep2"],
            priority=5,
        )
        assert config.description == "A test worker"
        assert config.max_retries == 3
        assert config.timeout_seconds == 60
        assert config.depends_on == ["dep1", "dep2"]
        assert config.priority == 5

    def test_model_dump(self):
        config = WorkerConfig(name="test", preset="default")
        data = config.model_dump()
        assert data["name"] == "test"
        assert data["preset"] == "default"


class TestWorkerResult:
    """WorkerResult model validation."""

    def test_minimal_result(self):
        result = WorkerResult(
            worker_name="test",
            preset="default",
            state=WorkerState.DONE,
        )
        assert result.data == {}
        assert result.signals == []
        assert result.analysis == ""
        assert result.metadata == {}
        assert result.ticker_results == {}
        assert result.error is None

    def test_result_with_all_fields(self):
        now = datetime.now(timezone.utc)
        result = WorkerResult(
            worker_name="test",
            preset="default",
            state=WorkerState.DONE,
            started_at=now,
            completed_at=now,
            error=None,
            data={"key": "value"},
            signals=[{"ticker": "AAPL", "action": "BUY"}],
            analysis="Test analysis",
            metadata={"meta_key": "meta_value"},
            ticker_results={"AAPL": {"score": 0.9}},
        )
        assert result.data["key"] == "value"
        assert len(result.signals) == 1
        assert result.analysis == "Test analysis"

    def test_failed_result(self):
        result = WorkerResult(
            worker_name="test",
            preset="default",
            state=WorkerState.FAILED,
            error="Something went wrong",
        )
        assert result.state == WorkerState.FAILED
        assert result.error == "Something went wrong"

    def test_model_dump_serializes(self):
        result = WorkerResult(
            worker_name="test",
            preset="default",
            state=WorkerState.DONE,
        )
        data = result.model_dump()
        assert data["worker_name"] == "test"
        assert data["state"] == "done"


class TestWorkerState:
    """WorkerState enum values."""

    def test_all_states_present(self):
        assert WorkerState.WAITING == "waiting"
        assert WorkerState.RUNNING == "running"
        assert WorkerState.DONE == "done"
        assert WorkerState.FAILED == "failed"
        assert WorkerState.BLOCKED == "blocked"
        assert WorkerState.RETRYING == "retrying"


class TestBaseSwarmWorker:
    """BaseSwarmWorker lifecycle tests."""

    def test_worker_initial_state(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        assert worker.state == WorkerState.WAITING
        assert worker.result is None
        assert worker._started_at is None

    def test_worker_is_ready_when_not_blocked(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        assert worker.is_ready is True

    def test_worker_not_ready_when_blocked(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        worker.state = WorkerState.BLOCKED
        assert worker.is_ready is False

    def test_run_sets_state_to_done(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        result = worker.run(symbols=["AAPL"], market_data={})
        assert worker.state == WorkerState.DONE
        assert result.state == WorkerState.DONE

    def test_run_sets_started_at_and_completed_at(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        worker.run(symbols=["AAPL"], market_data={})
        assert worker._started_at is not None
        assert worker.result.started_at is not None
        assert worker.result.completed_at is not None

    def test_run_stores_result(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                    data={"processed": True},
                )

        worker = TestWorker(config)
        worker.run(symbols=["AAPL"], market_data={})
        assert worker.result is not None
        assert worker.result.data["processed"] is True

    def test_run_with_retries_on_failure(self):
        config = WorkerConfig(name="test", preset="default", max_retries=2)
        call_count = 0

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise ValueError("transient error")
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        result = worker.run(symbols=["AAPL"], market_data={})
        assert call_count == 3
        assert worker.state == WorkerState.DONE
        assert result.state == WorkerState.DONE

    def test_run_fails_after_max_retries(self):
        config = WorkerConfig(name="test", preset="default", max_retries=1)

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                raise ValueError("permanent error")

        worker = TestWorker(config)
        result = worker.run(symbols=["AAPL"], market_data={})
        assert worker.state == WorkerState.FAILED
        assert result.state == WorkerState.FAILED
        assert "permanent error" in result.error

    def test_get_status(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        status = worker.get_status()
        assert status["name"] == "test"
        assert status["preset"] == "default"
        assert status["state"] == "waiting"

    def test_to_json_with_result(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        worker.run(symbols=["AAPL"], market_data={})
        json_str = worker.to_json()
        assert "test" in json_str
        assert "done" in json_str

    def test_to_json_without_result(self):
        config = WorkerConfig(name="test", preset="default")

        class TestWorker(BaseSwarmWorker):
            def execute(self, symbols, market_data, portfolio_state=None, **kwargs):
                return WorkerResult(
                    worker_name=self.config.name,
                    preset=self.config.preset,
                    state=WorkerState.DONE,
                )

        worker = TestWorker(config)
        json_str = worker.to_json()
        assert "test" in json_str
        assert "waiting" in json_str
