"""Swarm execution engine with DAG-based worker scheduling."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from trading_bot.swarm.base import BaseSwarmWorker, WorkerConfig, WorkerResult, WorkerState
from trading_bot.swarm.presets import get_preset
from trading_bot.swarm.results import (
    CommitteeDecision,
    SwarmRunSummary,
    WorkerVerdict,
)

logger = logging.getLogger(__name__)


class SwarmEngine:
    """DAG-based swarm execution engine.

    Manages worker lifecycle, dependency resolution, concurrent execution,
    and result aggregation for multi-agent trading analysis.
    """

    def __init__(self, preset_name: str, max_concurrent: int = 3):
        self.preset_name = preset_name
        self.max_concurrent = max_concurrent
        self.workers: dict[str, BaseSwarmWorker] = {}
        self.results: dict[str, WorkerResult] = {}
        self.run_summary: SwarmRunSummary | None = None
        self._lock = threading.Lock()

    def setup_workers(self, worker_classes: dict[str, type[BaseSwarmWorker]]) -> None:
        """Initialize workers from a preset configuration.

        Args:
            worker_classes: Mapping of worker name to worker class.
        """
        configs = get_preset(self.preset_name)

        for config in configs:
            if config.name not in worker_classes:
                logger.warning(
                    "Worker class '%s' not found for preset '%s', skipping",
                    config.name,
                    self.preset_name,
                )
                continue

            worker = worker_classes[config.name](config)
            self.workers[config.name] = worker

        logger.info(
            "Setup %d workers for preset '%s'",
            len(self.workers),
            self.preset_name,
        )

    def _get_ready_workers(self) -> list[str]:
        """Get workers whose dependencies are satisfied and are not yet running."""
        ready = []
        for name, worker in self.workers.items():
            if worker.state != WorkerState.WAITING:
                continue

            deps_satisfied = True
            for dep in worker.config.depends_on:
                dep_worker = self.workers.get(dep)
                if dep_worker is None:
                    deps_satisfied = False
                    break
                if dep_worker.state not in (WorkerState.DONE, WorkerState.FAILED):
                    deps_satisfied = False
                    break
                if dep_worker.state == WorkerState.FAILED:
                    worker.state = WorkerState.BLOCKED
                    deps_satisfied = False
                    break

            if deps_satisfied:
                ready.append(name)

        return ready

    def _execute_worker(
        self,
        worker: BaseSwarmWorker,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None,
        **kwargs: Any,
    ) -> WorkerResult:
        """Execute a single worker."""
        try:
            result = worker.run(
                symbols=symbols,
                market_data=market_data,
                portfolio_state=portfolio_state,
                **kwargs,
            )
            with self._lock:
                self.results[worker.config.name] = result
            return result
        except Exception as e:
            result = WorkerResult(
                worker_name=worker.config.name,
                preset=self.preset_name,
                state=WorkerState.FAILED,
                error=str(e),
            )
            with self._lock:
                self.results[worker.config.name] = result
            return result

    def run(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        portfolio_state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> SwarmRunSummary:
        """Execute the full swarm analysis.

        Args:
            symbols: List of ticker symbols to analyze.
            market_data: Pre-fetched market data keyed by symbol.
            portfolio_state: Current portfolio state if available.
            **kwargs: Additional context data.

        Returns:
            SwarmRunSummary with all worker results and aggregated decisions.
        """
        run_id = str(uuid.uuid4())[:8]
        started_at = datetime.now(timezone.utc)

        self.run_summary = SwarmRunSummary(
            run_id=run_id,
            preset_name=self.preset_name,
            symbols=symbols,
            total_workers=len(self.workers),
        )

        logger.info(
            "Starting swarm run %s with preset '%s' for %d symbols",
            run_id,
            self.preset_name,
            len(symbols),
        )

        # Execute workers in dependency order using thread pool
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as executor:
            while True:
                ready_workers = self._get_ready_workers()
                if not ready_workers:
                    # Check if all workers are done/failed/blocked
                    all_terminal = all(
                        w.state in (WorkerState.DONE, WorkerState.FAILED, WorkerState.BLOCKED)
                        for w in self.workers.values()
                    )
                    if all_terminal:
                        break
                    # Deadlock - workers waiting on failed dependencies
                    for w in self.workers.values():
                        if w.state == WorkerState.WAITING:
                            w.state = WorkerState.BLOCKED
                    break

                # Execute ready workers concurrently
                futures = {}
                for name in ready_workers:
                    worker = self.workers[name]
                    future = executor.submit(
                        self._execute_worker,
                        worker,
                        symbols,
                        market_data,
                        portfolio_state,
                        **kwargs,
                    )
                    futures[future] = name

                # Wait for all tasks in this batch
                for future in as_completed(futures):
                    name = futures[future]
                    worker = self.workers[name]
                    try:
                        result = future.result()
                        worker.state = WorkerState.DONE
                    except Exception as e:
                        worker.state = WorkerState.FAILED
                        self.results[name] = WorkerResult(
                            worker_name=name,
                            preset=self.preset_name,
                            state=WorkerState.FAILED,
                            error=f"Unexpected error: {e}",
                        )

        completed_at = datetime.now(timezone.utc)
        execution_time = (completed_at - started_at).total_seconds()

        # Populate summary
        self.run_summary.completed_at = completed_at
        self.run_summary.execution_time_seconds = execution_time
        self.run_summary.completed_workers = sum(
            1 for w in self.workers.values() if w.state == WorkerState.DONE
        )
        self.run_summary.failed_workers = sum(
            1 for w in self.workers.values() if w.state == WorkerState.FAILED
        )
        self.run_summary.blocked_workers = sum(
            1 for w in self.workers.values() if w.state == WorkerState.BLOCKED
        )

        # Aggregate decisions
        self.run_summary.decisions = self._aggregate_decisions(symbols)

        logger.info(
            "Swarm run %s completed in %.1fs: %d done, %d failed, %d blocked",
            run_id,
            execution_time,
            self.run_summary.completed_workers,
            self.run_summary.failed_workers,
            self.run_summary.blocked_workers,
        )

        return self.run_summary

    def _aggregate_decisions(
        self,
        symbols: list[str],
    ) -> dict[str, CommitteeDecision]:
        """Aggregate worker results into committee decisions per symbol."""
        decisions: dict[str, CommitteeDecision] = {}

        for symbol in symbols:
            votes_for = 0
            votes_against = 0
            votes_abstain = 0
            rationales: list[str] = []
            risk_factors: list[str] = []

            for worker_name, result in self.results.items():
                if symbol not in result.ticker_results:
                    continue

                ticker_result = result.ticker_results[symbol]
                # Handle both dict and object formats
                if isinstance(ticker_result, dict):
                    action = ticker_result.get("action", "HOLD")
                    rationale = ticker_result.get("reasons", [])
                    risk_factors_list = ticker_result.get("risk_factors", [])
                else:
                    if ticker_result.verdict is None:
                        continue
                    action = ticker_result.verdict.action
                    rationale = ticker_result.verdict.rationale or []
                    risk_factors_list = ticker_result.verdict.risk_factors or []

                if action == "BUY":
                    votes_for += 1
                elif action == "SELL":
                    votes_against += 1
                else:
                    votes_abstain += 1

                if rationale:
                    rationales.append(f"{worker_name}: {', '.join(rationale)}")
                if risk_factors_list:
                    risk_factors.extend(risk_factors_list)

            # Determine committee decision
            total_votes = votes_for + votes_against + votes_abstain
            if total_votes == 0:
                action = "HOLD"
                confidence = 0.0
            elif votes_for > votes_against:
                action = "BUY"
                confidence = votes_for / total_votes
            elif votes_against > votes_for:
                action = "SELL"
                confidence = votes_against / total_votes
            else:
                action = "HOLD"
                confidence = votes_abstain / total_votes if total_votes > 0 else 0.0

            key_rationale = rationales[0] if rationales else "No worker verdicts"

            decisions[symbol] = CommitteeDecision(
                decision="APPROVE" if action == "BUY" else ("REJECT" if action == "SELL" else "HOLD_FOR_MORE_INFO"),
                ticker=symbol,
                action=action,
                confidence=round(confidence, 4),
                votes_for=votes_for,
                votes_against=votes_against,
                votes_abstain=votes_abstain,
                total_workers=total_votes,
                key_rationale=key_rationale,
                risk_factors=risk_factors[:10],
            )

        return decisions
