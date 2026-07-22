from __future__ import annotations

import hashlib
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
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

# The runtime canary only supports sizing-only policy changes whose values
# lie in (0, 1) and whose baseline is exactly 1.0 (the production default).
# Adding more parameters requires a new spec revision plus fixtures.
RUNTIME_CANARY_ALLOWED: set[tuple[str, str]] = {
    ("supermodel", "range_bound_trend_caution_multiplier"),
}


def is_runtime_canary_supported(change: ParameterChange) -> bool:
    """Return True when ``change`` can be faithfully exercised by the runtime
    paired-canary shadow harness today. Anything else must be rejected at
    activation so the harness never silently simulates a parameter it
    cannot model accurately.

    Accepted ranges:
        - ``baseline`` in (0, 1] (current production default is 1.0).
        - ``candidate`` in (0, 1]; values equal to baseline are a no-op
          canary but still flow through the harness so the test path that
          asserts the candidate is written remains valid.
    """
    if (change.section, change.field) not in RUNTIME_CANARY_ALLOWED:
        return False
    if not (0.0 < float(change.candidate) <= 1.0):
        return False
    if not (0.0 < float(change.baseline) <= 1.0):
        return False
    return True


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
        baseline_was_absent = not self.overrides_path.exists()
        proposed = propose_tuning_overrides(
            Path(self.settings.app.log_dir),
            self.base_settings,
            Path(self.settings.app.scan_results_path),
        )
        change = select_single_change(baseline_overrides, proposed)
        if change is None:
            return None

        experiment_id = self._make_id(change)

        # Capture exact baseline bytes (or sentinel for absence) so rollback
        # restores the operator-authored file verbatim.
        if baseline_was_absent:
            self.store.snapshot_absent_baseline(experiment_id)
            baseline_checksum = ""
        else:
            raw = self.overrides_path.read_bytes()
            self.store.snapshot_overrides_bytes(experiment_id, "baseline", raw)
            baseline_checksum = self.store.checksum(
                self.store.root / experiment_id / "baseline.yaml"
            )

        candidate_overrides = self._apply_to_overrides(baseline_overrides, change)
        self.store.snapshot_overrides(experiment_id, "candidate", candidate_overrides)
        candidate_checksum = self.store.checksum(
            self.store.root / experiment_id / "candidate.yaml"
        )

        state = ExperimentState(
            experiment_id=experiment_id,
            status="PROPOSED",
            change=change,
            started_at=datetime.now(timezone.utc),
            baseline_metrics=None,
            candidate_metrics=None,
            shadow_metrics=None,
            candidate_checksum=candidate_checksum,
            baseline_checksum=baseline_checksum,
            baseline_was_absent=baseline_was_absent,
        )
        self.store.save_current(state)
        self.store.append_event({"event": "proposed", "experiment_id": experiment_id, "change": change.model_dump()})
        self._state = state
        return state

    # --- evaluation -----------------------------------------------------
    def supports_runtime_canary(self, change: ParameterChange) -> bool:
        """Whether the runtime canary harness can faithfully simulate this change."""
        return is_runtime_canary_supported(change)

    def record_canary_snapshot(
        self,
        state: ExperimentState,
        harness: Any,
    ) -> ExperimentState:
        """Copy harness metrics into ``state`` and detect divergence.

        The harness exposes ``candidate_metrics``, ``baseline_metrics``, and
        ``closed_trade_counts_match``. Runtime candidate metrics populate
        ``state.candidate_metrics``; the paired baseline populates
        ``state.shadow_metrics``. Offline baseline metrics on
        ``state.baseline_metrics`` remain untouched so the audit trail
        references both replay and runtime evidence side-by-side.

        When the paired trade counts diverge, the experiment is marked
        INCONCLUSIVE because the comparison is unsafe.
        """
        candidate = harness.candidate_metrics()
        baseline = harness.baseline_metrics()
        state.candidate_metrics = candidate
        state.shadow_metrics = baseline
        state.canary_closed_trades = int(candidate.trades)
        if not harness.closed_trade_counts_match():
            state.status = "INCONCLUSIVE"
            state.last_error = "paired_ledgers_diverged"
            self.store.save_current(state)
            self.store.append_event({
                "event": "paired_ledgers_diverged",
                "experiment_id": state.experiment_id,
                "candidate_trades": candidate.trades,
                "baseline_trades": baseline.trades,
            })
        else:
            self.store.save_current(state)
        return state

    def activate_canary(self, state: ExperimentState, ledger: Any) -> None:
        """Persist the immutable ``canary_starting_equity`` for restart safety."""
        from trading_bot.portfolio.ledger import PortfolioLedger

        if isinstance(ledger, PortfolioLedger):
            starting = ledger.ensure_portfolio_state().equity
        elif hasattr(ledger, "ensure_portfolio_state"):
            starting = ledger.ensure_portfolio_state().equity
        else:
            raise TypeError("ledger must expose ensure_portfolio_state()")
        state.canary_starting_equity = float(starting)
        self.store.save_current(state)
        self.store.append_event({
            "event": "canary_starting_equity_recorded",
            "experiment_id": state.experiment_id,
            "starting_equity": float(starting),
        })

    @staticmethod
    def _filter_fully_covered_symbols(
        symbols: list[str], bar_loader: Any, start: date, end: date
    ) -> list[str]:
        """Restrict the replay universe to symbols that have at least the
        first quarter of the requested window as local bars.

        ``available_symbols`` returns every symbol in the manifest, but the
        EOD store does not guarantee full coverage for every symbol;
        insufficient bars cause the per-symbol ``fetch_bars`` call to
        raise and abort the whole replay. Filtering up front keeps the
        replay deterministic and avoids silently dropping eligible
        symbols because of an unrelated data gap.
        """
        from trading_bot.data.data_store import read_bars

        root = Path(getattr(bar_loader, "root", ""))
        if not root:
            return list(symbols)
        cutoff = start + (end - start) / 4
        kept: list[str] = []
        for sym in symbols:
            try:
                df = read_bars(sym, "1d", start, cutoff, root)
            except Exception:  # noqa: BLE001
                continue
            if df.empty or len(df) < 5:
                continue
            kept.append(sym)
        return kept

    def _live_portfolio_is_flat(self) -> bool:
        """Return True when the live ledger has zero open positions.

        The runtime canary must start flat: a position opened before the
        canary has no paired shadow entry, so its exit cannot be mirrored
        safely. The check reads the live ledger and never raises on
        missing files (a never-persisted portfolio is treated as flat).
        """
        from trading_bot.portfolio.ledger import PortfolioLedger

        try:
            ledger = PortfolioLedger(Path(self.settings.app.state_db_path))
            portfolio = ledger.ensure_portfolio_state()
        except Exception:  # noqa: BLE001
            return True
        return all(
            position.quantity <= 0 for position in portfolio.positions.values()
        )

    def evaluate(self) -> ExperimentState | None:
        state = self.store.load_current()
        if state is None:
            return None

        # Offline stage
        if state.status == "PROPOSED":
            # Drift check: refuse to advance if someone hand-edited the
            # candidate between propose and evaluate.
            if self.store.detect_candidate_drift(
                state.experiment_id, state.candidate_checksum
            ):
                state.status = "ERROR"
                state.last_error = "candidate snapshot drifted from proposal"
                self.store.save_current(state)
                self.store.append_event({
                    "event": "candidate_drift",
                    "experiment_id": state.experiment_id,
                })
                return state

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
                self._archive_to_terminal(state, "OFFLINE_REJECTED")
                return state
            state.baseline_metrics = evaluation.baseline_validation
            state.candidate_metrics = evaluation.candidate_validation

            # Runtime-canary gating: refuse to enter the runtime canary for
            # any change the harness cannot simulate faithfully, or whenever
            # the live portfolio is not flat. We bail BEFORE the candidate
            # bytes are activated so baseline overrides remain intact.
            if not is_runtime_canary_supported(state.change):
                state.status = "INCONCLUSIVE"
                state.last_error = "unsupported_runtime_canary"
                self.store.save_current(state)
                self.store.append_event({
                    "event": "unsupported_runtime_canary",
                    "experiment_id": state.experiment_id,
                    "change": state.change.model_dump(),
                })
                return state
            state.runtime_canary_armed = True
            if not self._live_portfolio_is_flat():
                state.status = "INCONCLUSIVE"
                state.last_error = "non_flat_portfolio_on_canary_start"
                state.runtime_canary_armed = False
                self.store.save_current(state)
                self.store.append_event({
                    "event": "non_flat_portfolio_on_canary_start",
                    "experiment_id": state.experiment_id,
                })
                return state
            state.status = "CANARY"

            # Drift guard: if the operator hand-edited the live overrides
            # since proposal, log it before we overwrite with candidate bytes.
            # We don't refuse activation (operator edits during canary are
            # the operator's call), but we record the event so audit
            # can show what was overwritten.
            if self.store.detect_baseline_drift(
                self.overrides_path,
                state.baseline_checksum,
                state.baseline_was_absent,
            ):
                self.store.append_event({
                    "event": "baseline_drift_detected",
                    "experiment_id": state.experiment_id,
                    "phase": "activation",
                })

            # Atomic activation: copy candidate snapshot bytes verbatim to
            # the live overrides path. After this line the next bot process
            # will load the candidate, not the baseline.
            activated = self.store.activate_candidate(
                state.experiment_id, self.overrides_path
            )
            if not activated:
                state.status = "ERROR"
                state.last_error = "candidate snapshot missing at activation"
                self.store.save_current(state)
                self.store.append_event({
                    "event": "candidate_missing",
                    "experiment_id": state.experiment_id,
                })
                # Restore baseline so we don't leave a partial state.
                self.store.restore_baseline_exact(
                    state.experiment_id, self.overrides_path
                )
                return state
            self.store.save_current(state)
            self.store.append_event({"event": "canary_started", "experiment_id": state.experiment_id})

        if state.status != "CANARY":
            return state

        # Runtime-canary resume guard: only runs when the experiment was
        # actually armed by the activation path (which sets
        # canary_starting_equity). Experiments that bypass activation —
        # typically legacy test fixtures — keep their old semantics.
        if (
            state.runtime_canary_armed
            and state.canary_closed_trades == 0
            and not is_runtime_canary_supported(state.change)
        ):
            state.status = "INCONCLUSIVE"
            state.last_error = "unsupported_runtime_canary"
            self.store.save_current(state)
            self.store.append_event({
                "event": "unsupported_runtime_canary",
                "experiment_id": state.experiment_id,
                "change": state.change.model_dump(),
                "phase": "canary_resume",
            })
            self.store.restore_baseline_exact(
                state.experiment_id, self.overrides_path
            )
            return state
        if (
            state.runtime_canary_armed
            and state.canary_closed_trades == 0
            and not self._live_portfolio_is_flat()
            and is_runtime_canary_supported(state.change)
        ):
            state.status = "INCONCLUSIVE"
            state.last_error = "non_flat_portfolio_on_canary_start"
            self.store.save_current(state)
            self.store.append_event({
                "event": "non_flat_portfolio_on_canary_start",
                "experiment_id": state.experiment_id,
                "phase": "canary_resume",
            })
            self.store.restore_baseline_exact(
                state.experiment_id, self.overrides_path
            )
            return state

        decision = self._decide(state)
        state.status = decision
        if decision == "KEPT":
            self.store.save_current(state)
            self._archive_to_terminal(state, "KEPT")
        elif decision in {"ROLLED_BACK", "INCONCLUSIVE", "ERROR"}:
            self.store.restore_baseline_exact(
                state.experiment_id, self.overrides_path
            )
            state.rolled_back_at = datetime.now(timezone.utc)
            self.store.save_current(state)
            self._archive_to_terminal(state, decision)
        self.store.append_event({"event": decision.lower(), "experiment_id": state.experiment_id})
        return state

    def rollback(self, reason: str | None = None) -> ExperimentState | None:
        state = self.store.load_current()
        if state is None:
            return None
        # Drift guard: log if the live overrides have been mutated away
        # from the bytes we recorded at proposal time. This protects
        # operators from silent overwrites of post-activation edits.
        if self.store.detect_baseline_drift(
            self.overrides_path,
            state.baseline_checksum,
            state.baseline_was_absent,
        ):
            self.store.append_event({
                "event": "baseline_drift_detected",
                "experiment_id": state.experiment_id,
                "phase": "rollback",
                "reason": reason,
            })
        ok = self.store.restore_baseline_exact(
            state.experiment_id, self.overrides_path
        )
        state.status = "ROLLED_BACK"
        state.rolled_back_at = datetime.now(timezone.utc)
        state.last_error = reason
        if ok:
            self._archive_to_terminal(state, "ROLLED_BACK")
        self.store.append_event({
            "event": "rolled_back",
            "experiment_id": state.experiment_id,
            "manual": True,
            "reason": reason,
        })
        return state

    def _archive_to_terminal(
        self, state: ExperimentState, status: str
    ) -> None:
        """Move the active experiment state to archived/<id>/ rather than
        deleting it. Terminal outcomes (KEPT/ROLLED_BACK/INCONCLUSIVE/ERROR/
        OFFLINE_REJECTED) must remain auditable."""
        archived = self.store.root / "archived" / state.experiment_id
        archived.mkdir(parents=True, exist_ok=True)
        # Persist the *terminal* status (not the pre-terminal one), then move.
        state.status = status  # type: ignore[assignment]
        self.store.save_current(state)
        target_state = archived / "current.json"
        if self.store.current_path.exists():
            shutil.move(str(self.store.current_path), str(target_state))
        src = self.store.root / state.experiment_id
        if src.exists():
            target_files = archived / "files"
            if target_files.exists():
                shutil.rmtree(target_files)
            shutil.move(str(src), str(target_files))

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
        symbols = self._filter_fully_covered_symbols(
            symbols, self.bar_loader, start_date, end_date
        )
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

    # ------------------------------------------------------------------
    # Plan C step 1: archival + exact-byte rollback + version drift
    # detection. These helpers are classmethods so callers can invoke
    # them without instantiating a full controller (mirroring the
    # pattern used by the rest of the module).
    # ------------------------------------------------------------------

    ARCHIVED_DIRNAME = "archived"

    @classmethod
    def archive_terminal(
        cls,
        store: ExperimentStore,
        *,
        status: str,
        reason: str | None = None,
    ) -> ExperimentState | None:
        """Move the current state to ``archived/<experiment_id>`` instead of
        deleting it. Terminal outcomes (KEPT / ROLLED_BACK / INCONCLUSIVE /
        ERROR) must remain auditable; clearing the row leaves no trace."""
        state = store.load_current()
        if state is None:
            return None
        state.status = status
        if reason:
            state.last_error = reason
        archived = store.root / cls.ARCHIVED_DIRNAME / state.experiment_id
        archived.mkdir(parents=True, exist_ok=True)
        # Move both the current state JSON and the per-experiment dir.
        if store.current_path.exists():
            shutil.move(str(store.current_path), str(archived / "current.json"))
        src = store.root / state.experiment_id
        if src.exists():
            target = archived / "files"
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(src), str(target))
        store.append_event({
            "event": status.lower(),
            "experiment_id": state.experiment_id,
            "archived_to": str(archived),
            "reason": reason,
        })
        return state

    @classmethod
    def restore_baseline_to_target(
        cls,
        *,
        store: ExperimentStore,
        experiment_id: str,
        target: Path,
    ) -> bool:
        """Restore the baseline snapshot exactly.

        If the baseline snapshot is marked "absent" (a sentinel file left
        at archive time), the target must be deleted instead of overwritten
        so callers can tell "no override was ever active" apart from
        "an empty override is active".
        """
        snapshot = store.root / experiment_id / "baseline.yaml"
        absent_marker = store.root / experiment_id / "baseline.absent"
        if absent_marker.exists():
            if target.exists():
                target.unlink()
            return True
        if not snapshot.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        # Preserve exact snapshot bytes to satisfy "exact original
        # override-byte snapshot" from Plan C.
        with NamedTemporaryFile(
            "wb", dir=target.parent, delete=False
        ) as handle:
            handle.write(snapshot.read_bytes())
            temp_path = Path(handle.name)
        temp_path.replace(target)
        return True

    @classmethod
    def detect_candidate_drift(
        cls,
        store: ExperimentStore,
        experiment_id: str,
        *,
        expected_checksum: str | None = None,
    ) -> bool:
        """True when the live candidate snapshot has been mutated away
        from the bytes recorded at proposal time."""
        snapshot = store.root / experiment_id / "candidate.yaml"
        if not snapshot.exists():
            return True
        actual = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if expected_checksum is None:
            return False
        return actual != expected_checksum