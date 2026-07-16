from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trading_bot.config.settings import Settings
from trading_bot.learning.experiments.models import (
    ExperimentState,
    MetricSet,
    ParameterChange,
)
from trading_bot.learning.experiments.proposal import select_single_change
from trading_bot.learning.experiments.replay import (
    OfflineEvaluation,
    StoredBarLoader,
    evaluate_offline,
)
from trading_bot.learning.experiments.store import ExperimentStore

MIN_OFFLINE_TRADES = 20
MIN_CANARY_TRADES = 20
EARLY_ROLLBACK_TRADES = 10
PF_DELTA = 0.10
DRAWDOWN_BUFFER_PP = 5.0
EARLY_DRAWDOWN_BUFFER_PP = 10.0
EARLY_PF_FLOOR = 0.50
TIMEOUT_SESSIONS = 10
HEALTH_STALE_SECONDS = 7200


class ExperimentController:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ExperimentStore,
        bar_loader: StoredBarLoader | None,
        overrides_path: Path,
        base_settings: Settings | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bar_loader = bar_loader
        self.overrides_path = Path(overrides_path)
        self.base_settings = base_settings or settings
        self._state: ExperimentState | None = None

    def _set_state(self, state: ExperimentState | None) -> None:
        self._state = state
        if state is not None:
            self.store.save_current(state)

    # --- proposals ------------------------------------------------------
    def propose(self) -> ExperimentState | None:
        from trading_bot.learning.tuning_overrides import propose_tuning_overrides

        if self.store.load_current() is not None:
            return None

        baseline_overrides = self._current_overrides()
        proposed = propose_tuning_overrides(
            Path(self.settings.app.log_dir),
            self.base_settings,
            Path(self.settings.app.scan_results_path),
        )
        change = select_single_change(baseline_overrides, proposed)
        if change is None:
            return None

        experiment_id = self._make_id(change)
        self.store.snapshot_overrides(experiment_id, "baseline", baseline_overrides)
        candidate_overrides = self._apply_to_overrides(baseline_overrides, change)
        self.store.snapshot_overrides(experiment_id, "candidate", candidate_overrides)
        state = ExperimentState(
            experiment_id=experiment_id,
            status="PROPOSED",
            change=change,
            started_at=datetime.now(timezone.utc),
            baseline_metrics=None,
            candidate_metrics=None,
            shadow_metrics=None,
        )
        self.store.save_current(state)
        self.store.append_event({"event": "proposed", "experiment_id": experiment_id, "change": change.model_dump()})
        self._state = state
        return state

    # --- evaluation -----------------------------------------------------
    def evaluate(self) -> ExperimentState | None:
        state = self.store.load_current()
        if state is None:
            return None

        # Offline stage
        if state.status == "PROPOSED":
            evaluation = self._run_offline(state)
            self.store.append_event({
                "event": "offline_evaluated",
                "experiment_id": state.experiment_id,
                "accepted": evaluation.accepted,
                "reasons": evaluation.reasons,
            })
            if not evaluation.accepted:
                state.status = "OFFLINE_REJECTED"
                state.candidate_metrics = evaluation.candidate_validation
                state.baseline_metrics = evaluation.baseline_validation
                self.store.save_current(state)
                self.store.clear_current()
                return state
            state.baseline_metrics = evaluation.baseline_validation
            state.candidate_metrics = evaluation.candidate_validation
            state.status = "CANARY"
            self.store.save_current(state)
            self.store.append_event({"event": "canary_started", "experiment_id": state.experiment_id})

        if state.status != "CANARY":
            return state

        decision = self._decide(state)
        state.status = decision
        if decision == "KEPT":
            overrides = self._current_overrides()
            self.store.write_overrides_atomic(self.overrides_path, overrides)
            self.store.save_current(state)
        elif decision in {"ROLLED_BACK", "INCONCLUSIVE", "ERROR"}:
            self.store.restore_baseline(state.experiment_id, self.overrides_path)
            state.rolled_back_at = datetime.now(timezone.utc)
            self.store.save_current(state)
            self.store.clear_current()
        self.store.append_event({"event": decision.lower(), "experiment_id": state.experiment_id})
        return state

    def rollback(self, reason: str | None = None) -> ExperimentState | None:
        state = self.store.load_current()
        if state is None:
            return None
        ok = self.store.restore_baseline(state.experiment_id, self.overrides_path)
        state.status = "ROLLED_BACK"
        state.rolled_back_at = datetime.now(timezone.utc)
        state.last_error = reason
        if ok:
            self.store.clear_current()
        self.store.append_event({
            "event": "rolled_back",
            "experiment_id": state.experiment_id,
            "manual": True,
            "reason": reason,
        })
        return state

    def status(self) -> dict[str, Any]:
        state = self.store.load_current()
        if state is None:
            return {"active": False}
        return {
            "active": True,
            "experiment_id": state.experiment_id,
            "status": state.status,
            "change": state.change.model_dump(),
            "canary_closed_trades": state.canary_closed_trades,
            "market_sessions": state.market_sessions,
            "baseline_metrics": state.baseline_metrics.model_dump() if state.baseline_metrics else None,
            "candidate_metrics": state.candidate_metrics.model_dump() if state.candidate_metrics else None,
            "shadow_metrics": state.shadow_metrics.model_dump() if state.shadow_metrics else None,
        }

    # --- internals ------------------------------------------------------
    def _decide(self, state: ExperimentState) -> str:
        if len(state.market_sessions) >= TIMEOUT_SESSIONS and state.canary_closed_trades < MIN_CANARY_TRADES:
            return "INCONCLUSIVE"

        candidate = state.candidate_metrics
        shadow = state.shadow_metrics
        if candidate is None or shadow is None:
            return state.status

        if state.canary_closed_trades >= EARLY_ROLLBACK_TRADES:
            if candidate.profit_factor < EARLY_PF_FLOOR:
                return "ROLLED_BACK"
            if candidate.max_drawdown_pct > shadow.max_drawdown_pct + EARLY_DRAWDOWN_BUFFER_PP:
                return "ROLLED_BACK"

        if state.canary_closed_trades >= MIN_CANARY_TRADES:
            if candidate.profit_factor < shadow.profit_factor + PF_DELTA:
                return "ROLLED_BACK"
            if candidate.net_pnl <= shadow.net_pnl:
                return "ROLLED_BACK"
            if candidate.max_drawdown_pct > shadow.max_drawdown_pct + DRAWDOWN_BUFFER_PP:
                return "ROLLED_BACK"
            return "KEPT"

        return state.status

    def _run_offline(self, state: ExperimentState) -> OfflineEvaluation:
        if self.bar_loader is None:
            raise RuntimeError("StoredBarLoader required for offline evaluation")
        end_date = date.today()
        start_date = end_date.replace(year=end_date.year - 2)
        symbols = self.bar_loader.available_symbols()
        return evaluate_offline(
            settings=self.settings,
            change=state.change,
            symbols=symbols,
            start=start_date,
            end=end_date,
            bar_loader=self.bar_loader,
        )

    def _current_overrides(self) -> dict[str, dict[str, float]]:
        if self.overrides_path.exists():
            return yaml.safe_load(self.overrides_path.read_text(encoding="utf-8")) or {}
        return {
            "supermodel": {
                "support_threshold": self.settings.supermodel.support_threshold,
                "block_threshold": self.settings.supermodel.block_threshold,
                "counter_veto_weight": self.settings.supermodel.counter_veto_weight,
            },
            "strategy_tracker": {
                "window": self.settings.strategy_tracker.window,
                "min_win_rate": self.settings.strategy_tracker.min_win_rate,
                "full_allocation_rate": self.settings.strategy_tracker.full_allocation_rate,
            },
        }

    def _apply_to_overrides(
        self,
        overrides: dict[str, dict[str, float]],
        change: ParameterChange,
    ) -> dict[str, dict[str, float]]:
        result = {section: dict(values) for section, values in overrides.items()}
        result.setdefault(change.section, {})[change.field] = change.candidate
        return result

    def _make_id(self, change: ParameterChange) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{stamp}__{change.section}.{change.field}-{change.baseline:g}-to-{change.candidate:g}"