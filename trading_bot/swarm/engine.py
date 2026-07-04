"""Swarm execution engine with DAG-based worker scheduling."""

from __future__ import annotations

import logging
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trading_bot.swarm.base import BaseSwarmWorker, WorkerConfig, WorkerResult, WorkerState
from trading_bot.swarm.presets import get_preset
from trading_bot.swarm.results import (
    CommitteeDecision,
    SignalVote,
    SwarmRunSummary,
    WorkerVerdict,
)

logger = logging.getLogger(__name__)


class SwarmEngine:
    """DAG-based swarm execution engine.

    Manages worker lifecycle, dependency resolution, concurrent execution,
    and result aggregation for multi-agent trading analysis.
    """

    def __init__(self, preset_name: str, max_concurrent: int = 3) -> None:
        self.preset_name = preset_name
        self.max_concurrent = max_concurrent
        self.workers: dict[str, BaseSwarmWorker] = {}
        self.results: dict[str, WorkerResult] = {}
        self.run_summary: SwarmRunSummary | None = None
        self._lock = threading.Lock()

    def _log_worker_vote(
        self,
        vote_log_path: Path,
        *,
        ticker: str,
        worker_name: str,
        action: str,
        confidence: float,
        accuracy_weight: float,
    ) -> None:
        vote_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "preset": self.preset_name,
            "ticker": ticker,
            "worker_name": worker_name,
            "action": action,
            "confidence": round(confidence, 4),
            "accuracy_weight": round(accuracy_weight, 4),
        }
        with vote_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")

    def setup_workers(self, worker_classes: dict[str, type[BaseSwarmWorker]]) -> None:
        """Initialize workers from a preset configuration.

        Args:
            worker_classes: Mapping of worker name to worker class.
        """
        configs = get_preset(self.preset_name)
        self.workers = {}
        self.results = {}
        self.run_summary = None

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
            with self._lock:
                worker_results = dict(self.results)
            result = worker.run(
                symbols=symbols,
                market_data=market_data,
                portfolio_state=portfolio_state,
                worker_results=worker_results,
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
        self.results = {}
        for worker in self.workers.values():
            worker.state = WorkerState.WAITING
            worker.result = None

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
                        worker.state = result.state
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
        vote_log = kwargs.get("vote_log_path")
        vote_log_path = Path(vote_log) if vote_log else None
        self.run_summary.decisions = self._aggregate_decisions(symbols, vote_log_path=vote_log_path)

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
        vote_log_path: Path | None = None,
    ) -> dict[str, CommitteeDecision]:
        """Aggregate worker results into committee decisions per symbol."""
        decisions: dict[str, CommitteeDecision] = {}

        for symbol in symbols:
            votes_for = 0
            votes_against = 0
            votes_abstain = 0
            weighted_for = 0.0
            weighted_against = 0.0
            weighted_abstain = 0.0
            rationales: list[str] = []
            risk_factors: list[str] = []
            supporting_signals: list[SignalVote] = []
            opposing_signals: list[SignalVote] = []

            for worker_name in self.workers:
                worker_weight = float(self.workers[worker_name].config.accuracy_weight)
                result = self.results.get(worker_name)
                if result is None or symbol not in result.ticker_results:
                    votes_abstain += 1
                    weighted_abstain += worker_weight
                    continue

                ticker_result = result.ticker_results[symbol]
                signal_vote = None
                # Handle both dict and object formats
                if isinstance(ticker_result, dict):
                    action = ticker_result.get("action", "HOLD")
                    rationale = ticker_result.get("reasons", [])
                    metadata = ticker_result.get("metadata", {})
                    risk_factors_list = ticker_result.get("risk_factors", [])
                    try:
                        signal_vote = SignalVote.model_validate(ticker_result)
                    except (TypeError, ValueError):
                        signal_vote = None
                else:
                    if ticker_result.verdict is None:
                        votes_abstain += 1
                        continue
                    action = ticker_result.verdict.action
                    rationale = ticker_result.verdict.rationale or []
                    metadata = {}
                    risk_factors_list = ticker_result.verdict.risk_factors or []

                if action == "BUY":
                    votes_for += 1
                    weighted_for += worker_weight
                    if signal_vote is not None:
                        supporting_signals.append(signal_vote)
                elif action == "SELL":
                    votes_against += 1
                    weighted_against += worker_weight
                    if signal_vote is not None:
                        opposing_signals.append(signal_vote)
                else:
                    votes_abstain += 1
                    weighted_abstain += worker_weight

                if vote_log_path is not None:
                    logged_confidence = signal_vote.confidence if signal_vote is not None else 0.0
                    self._log_worker_vote(
                        vote_log_path,
                        ticker=symbol,
                        worker_name=worker_name,
                        action=action,
                        confidence=logged_confidence,
                        accuracy_weight=worker_weight,
                    )

                if rationale:
                    rationales.append(f"{worker_name}: {', '.join(rationale)}")
                handoff = _handoff_rationale(worker_name, metadata)
                if handoff:
                    rationales.append(handoff)
                    risk_factors.append(handoff)
                if risk_factors_list:
                    risk_factors.extend(risk_factors_list)

            # Determine committee decision
            total_votes = votes_for + votes_against + votes_abstain
            total_weight = weighted_for + weighted_against + weighted_abstain
            if total_weight == 0:
                action = "HOLD"
                confidence = 0.0
            elif weighted_for > weighted_against:
                action = "BUY"
                confidence = weighted_for / total_weight
            elif weighted_against > weighted_for:
                action = "SELL"
                confidence = weighted_against / total_weight
            else:
                action = "HOLD"
                confidence = weighted_abstain / total_weight if total_weight > 0 else 0.0

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
                supporting_signals=supporting_signals,
                opposing_signals=opposing_signals,
                risk_factors=risk_factors[:10],
            )

        return decisions


def _handoff_rationale(worker_name: str, metadata: dict[str, Any]) -> str | None:
    if worker_name != "risk_manager":
        return None
    technical = metadata.get("technical_action")
    fundamental = metadata.get("fundamental_action")
    if not technical and not fundamental:
        return None
    return f"risk_manager handoff: technical={technical or 'n/a'} fundamental={fundamental or 'n/a'}"
