"""Research autopilot engine.

Manages the research loop:
1. Generate hypotheses from benching results or manual input
2. Run backtests for each hypothesis
3. Evaluate results
4. Generate new hypotheses based on findings
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from trading_bot.research.models import (
    ExperimentResult,
    Hypothesis,
    HypothesisCategory,
    HypothesisStatus,
    ResearchCycle,
)
from trading_bot.research.store import ResearchStore

logger = logging.getLogger(__name__)


class ResearchEngine:
    """Research autopilot engine.

    Manages the hypothesis → backtest → evaluate → learn loop.
    """

    def __init__(self, store: ResearchStore | None = None):
        self.store = store or ResearchStore()
        self._cycles: list[ResearchCycle] = []

    def create_hypothesis(
        self,
        title: str,
        description: str,
        category: HypothesisCategory = HypothesisCategory.CUSTOM,
        parameters: dict[str, Any] | None = None,
        expected_outcome: str = "",
        parent_id: str | None = None,
    ) -> Hypothesis:
        """Create a new hypothesis."""
        hypothesis = Hypothesis(
            title=title,
            description=description,
            category=category,
            parameters=parameters or {},
            expected_outcome=expected_outcome,
            parent_hypothesis_id=parent_id,
        )
        self.store.save_hypothesis(hypothesis)
        logger.info("Created hypothesis: %s", title)
        return hypothesis

    def run_cycle(
        self,
        hypothesis: Hypothesis,
        backtest_fn: Any,
        evaluate_fn: Any | None = None,
    ) -> ResearchCycle:
        """Run a complete research cycle for a hypothesis.

        Args:
            hypothesis: The hypothesis to test.
            backtest_fn: Function that runs a backtest and returns metrics dict.
                Signature: (hypothesis) -> dict with keys like:
                    total_return, win_rate, sharpe_ratio, max_drawdown,
                    total_trades, profit_factor, avg_trade_pnl
            evaluate_fn: Optional function to evaluate results.
                Signature: (hypothesis, metrics) -> str (evaluation summary)

        Returns:
            ResearchCycle with hypothesis, result, and evaluation.
        """
        # Mark hypothesis as running
        hypothesis.mark_running()
        self.store.save_hypothesis(hypothesis)

        # Run backtest
        logger.info("Running backtest for: %s", hypothesis.title)
        metrics = backtest_fn(hypothesis)

        # Build experiment result
        experiment_result = ExperimentResult(
            hypothesis_id=hypothesis.id,
            backtest_start=hypothesis.parameters.get("start_date", ""),
            backtest_end=hypothesis.parameters.get("end_date", ""),
            symbols=hypothesis.parameters.get("symbols", []),
            total_return=metrics.get("total_return", 0.0),
            win_rate=metrics.get("win_rate", 0.0),
            sharpe_ratio=metrics.get("sharpe_ratio", 0.0),
            max_drawdown=metrics.get("max_drawdown", 0.0),
            total_trades=metrics.get("total_trades", 0),
            profit_factor=metrics.get("profit_factor", 0.0),
            avg_trade_pnl=metrics.get("avg_trade_pnl", 0.0),
            metrics=metrics,
        )
        self.store.save_experiment_result(experiment_result)

        # Evaluate
        if evaluate_fn:
            evaluation = evaluate_fn(hypothesis, metrics)
        else:
            evaluation = self._default_evaluate(hypothesis, metrics)

        # Update hypothesis status
        if experiment_result.is_successful():
            hypothesis.mark_passed(evaluation)
        elif metrics.get("win_rate", 0) < 0.35:
            hypothesis.mark_failed(evaluation)
        else:
            hypothesis.mark_inconclusive(evaluation)

        self.store.save_hypothesis(hypothesis)

        # Build cycle
        cycle = ResearchCycle(
            hypothesis=hypothesis,
            experiment_result=experiment_result,
            evaluation=evaluation,
        )
        self.store.save_cycle(cycle)
        self._cycles.append(cycle)

        logger.info(
            "Cycle complete: %s -> %s",
            hypothesis.title,
            hypothesis.status.value,
        )
        return cycle

    def run_pending_hypotheses(
        self,
        backtest_fn: Any,
        max_cycles: int = 10,
        evaluate_fn: Any | None = None,
    ) -> list[ResearchCycle]:
        """Run all pending hypotheses up to max_cycles.

        Args:
            backtest_fn: Backtest function.
            max_cycles: Maximum number of cycles to run.
            evaluate_fn: Optional evaluation function.

        Returns:
            List of completed ResearchCycles.
        """
        pending = self.store.list_hypotheses(
            status=HypothesisStatus.PENDING, limit=max_cycles
        )
        cycles = []

        for hypothesis in pending[:max_cycles]:
            try:
                cycle = self.run_cycle(
                    hypothesis, backtest_fn, evaluate_fn
                )
                cycles.append(cycle)
            except Exception as e:
                logger.error("Cycle failed for %s: %s", hypothesis.title, e)
                self.store.update_hypothesis_status(
                    hypothesis.id,
                    HypothesisStatus.FAILED,
                    f"Error: {e}",
                )

        return cycles

    def auto_generate_hypotheses_from_benching(
        self, benching_results: dict[str, Any]
    ) -> list[Hypothesis]:
        """Generate hypotheses from alpha benching results.

        Args:
            benching_results: Results from alpha-bench command.

        Returns:
            List of generated hypotheses.
        """
        hypotheses = []

        # Generate hypotheses for "alive" factors
        for zoo_name, zoo_data in benching_results.items():
            if not isinstance(zoo_data, dict):
                continue

            factors = zoo_data.get("factors", [])
            for factor_data in factors:
                if factor_data.get("categorization") == "alive":
                    factor_name = factor_data.get("factor_name", "")
                    ic_ir = factor_data.get("ic_ir", 0)

                    hypothesis = self.create_hypothesis(
                        title=f"Optimize {factor_name} parameters",
                        description=(
                            f"Factor '{factor_name}' has IC IR of {ic_ir:.2f}. "
                            f"Test parameter variations to improve performance."
                        ),
                        category=HypothesisCategory.FACTOR_TWEAK,
                        parameters={
                            "factor_name": factor_name,
                            "base_ic_ir": ic_ir,
                            "zoo": zoo_name,
                        },
                        expected_outcome=f"Improve IC IR from {ic_ir:.2f} to >{ic_ir * 1.2:.2f}",
                    )
                    hypotheses.append(hypothesis)

        # Generate hypotheses for "reversed" factors
        for zoo_name, zoo_data in benching_results.items():
            if not isinstance(zoo_data, dict):
                continue

            factors = zoo_data.get("factors", [])
            for factor_data in factors:
                if factor_data.get("categorization") == "reversed":
                    factor_name = factor_data.get("factor_name", "")
                    ic_ir = factor_data.get("ic_ir", 0)

                    hypothesis = self.create_hypothesis(
                        title=f"Reverse signal for {factor_name}",
                        description=(
                            f"Factor '{factor_name}' has negative IC IR of {ic_ir:.2f}. "
                            f"Test reversing the signal direction."
                        ),
                        category=HypothesisCategory.FACTOR_TWEAK,
                        parameters={
                            "factor_name": factor_name,
                            "base_ic_ir": ic_ir,
                            "action": "reverse_signal",
                        },
                        expected_outcome=f"Reversed signal achieves positive IC IR > 0.2",
                    )
                    hypotheses.append(hypothesis)

        logger.info(
            "Generated %d hypotheses from benching results", len(hypotheses)
        )
        return hypotheses

    def get_stats(self) -> dict[str, Any]:
        """Get research statistics."""
        return self.store.get_stats()

    def list_cycles(self, limit: int = 20) -> list[ResearchCycle]:
        """List research cycles."""
        return self.store.list_cycles(limit)

    # --- Internal ---

    def _default_evaluate(
        self, hypothesis: Hypothesis, metrics: dict[str, Any]
    ) -> str:
        """Default evaluation logic."""
        win_rate = metrics.get("win_rate", 0)
        sharpe = metrics.get("sharpe_ratio", 0)
        drawdown = metrics.get("max_drawdown", 0)
        profit_factor = metrics.get("profit_factor", 0)

        parts = []
        parts.append(f"Win rate: {win_rate:.1%}")
        parts.append(f"Sharpe: {sharpe:.2f}")
        parts.append(f"Max DD: {drawdown:.1%}")
        parts.append(f"Profit factor: {profit_factor:.2f}")

        if win_rate >= 0.45 and sharpe >= 0.5:
            verdict = "PASSED"
        elif win_rate < 0.35:
            verdict = "FAILED - too many losing trades"
        elif drawdown > 0.30:
            verdict = "FAILED - excessive drawdown"
        else:
            verdict = "INCONCLUSIVE - needs more data"

        return f"{verdict}: {'; '.join(parts)}"
