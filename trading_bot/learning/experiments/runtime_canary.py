"""Runtime canary context: bridges live trading paths to the paired shadow harness.

A ``RuntimeCanaryContext`` owns the live view of an experiment in
``CANARY``: the loaded ``ExperimentState``, the ``ExperimentController``
for marking metrics and decisions, and the paired ``PairedShadowHarness``
that mirrors every BUY and SELL into two parallel ledgers.

The production lifecycle boundary is ``begin_runtime_canary(settings, ledger)``
and ``finish_runtime_canary(context)``. The canonical experiment root is
derived from ``<state-db-parent>/tuning_experiments`` so the production
path cannot silently return ``None`` when dependencies are missing. The
internal ``_build_canary_context_with_deps`` helper is the explicit
test-only construction seam — it accepts an optional ``controller`` and
``store`` so tests can inject a custom store, but the production API
never passes those parameters.

When no experiment is in CANARY, ``begin_runtime_canary`` returns
``None`` so the trading paths transparently fall back to the legacy
no-shadow behavior. Malformed or inaccessible active state raises
``RuntimeCanaryLifecycleError`` so corruption is observable rather than
silently equated with "no canary".

Failures during shadow persistence (disk errors, malformed JSONL, ledger
divergence) trigger :meth:`RuntimeCanaryContext.invalidate`, which marks
the experiment ``INCONCLUSIVE`` and allows live paper trading to
continue rather than blocking on observability issues.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from trading_bot.config.settings import Settings
from trading_bot.learning.experiments.controller import (
    ExperimentController,
    is_runtime_canary_supported,
)
from trading_bot.learning.experiments.models import (
    ExperimentState,
    ParameterChange,
)
from trading_bot.learning.experiments.shadow import PairedShadowHarness
from trading_bot.learning.experiments.store import ExperimentStore
from trading_bot.portfolio.ledger import PortfolioLedger

logger = logging.getLogger(__name__)


class RuntimeCanaryLifecycleError(Exception):
    """Raised when the runtime canary lifecycle cannot proceed.

    Differentiates malformed/inaccessible active state from the
    benign "no active canary" path so callers can fail loudly rather
    than silently treating corruption as inactivity.
    """


@dataclass
class RuntimeCanaryContext:
    """Live trading view of a CANARY experiment with paired shadow ledgers."""

    state: ExperimentState
    controller: ExperimentController
    store: ExperimentStore
    harness: PairedShadowHarness
    artifacts_dir: Path
    starting_cash: float
    _invalidated_reasons: list[str] = field(default_factory=list)

    def record_entry(
        self,
        *,
        ticker: str,
        baseline_quantity: int,
        candidate_quantity: int,
        fill_price: float,
        fees: float,
        operation_id: str = "",
        session_date: str | None = None,
    ) -> None:
        if self._invalidated_reasons:
            return
        try:
            self.harness.record_entry(
                operation_id=operation_id,
                ticker=ticker,
                baseline_quantity=baseline_quantity,
                candidate_quantity=candidate_quantity,
                fill_price=fill_price,
                fees=fees,
            )
            self._maybe_add_session(session_date)
        except Exception as exc:  # noqa: BLE001
            self.invalidate(f"record_entry_failure: {exc!s}")

    def record_exit(
        self,
        *,
        ticker: str,
        candidate_quantity: int,
        fill_price: float,
        fees: float,
        operation_id: str = "",
        session_date: str | None = None,
    ) -> None:
        if self._invalidated_reasons:
            return
        try:
            self.harness.record_exit(
                operation_id=operation_id,
                ticker=ticker,
                candidate_quantity=candidate_quantity,
                fill_price=fill_price,
                fees=fees,
            )
            self._maybe_add_session(session_date)
        except Exception as exc:  # noqa: BLE001
            self.invalidate(f"record_exit_failure: {exc!s}")

    def snapshot(self) -> None:
        if self._invalidated_reasons:
            return
        try:
            self.controller.record_canary_snapshot(self.state, self.harness)
        except Exception as exc:  # noqa: BLE001
            self.invalidate(f"snapshot_failure: {exc!s}")

    def invalidate(self, reason: str) -> None:
        """Mark the canary INCONCLUSIVE and record the reason.

        Live paper trading continues; the controller restores baseline
        overrides and archives the experiment on the next ``evaluate()``
        call.
        """
        if reason in self._invalidated_reasons:
            return
        self._invalidated_reasons.append(reason)
        self.state.status = "INCONCLUSIVE"
        self.state.last_error = reason
        self.state.rolled_back_at = datetime.now(timezone.utc)
        try:
            self.store.save_current(self.state)
            self.store.append_event({
                "event": "runtime_canary_invalidated",
                "experiment_id": self.state.experiment_id,
                "reason": reason,
            })
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist canary invalidation")
        logger.warning(
            "Runtime canary %s invalidated: %s",
            self.state.experiment_id,
            reason,
        )

    def _maybe_add_session(self, session_date: str | None) -> None:
        if not session_date:
            return
        if session_date in self.state.market_sessions:
            return
        new_sessions = list(self.state.market_sessions) + [session_date]
        try:
            self.state.market_sessions = new_sessions
            self.store.save_current(self.state)
        except Exception as exc:  # noqa: BLE001
            self.invalidate(f"session_persist_failure: {exc!s}")


def _resolve_artifacts_dir(store: ExperimentStore) -> Path:
    """Compute a per-experiment artifacts directory under the experiments root."""
    state = store.load_current()
    if state is None:
        return store.root / "_unknown"
    return store.root / state.experiment_id


def _resolve_starting_cash(state: ExperimentState, ledger: object | None) -> float:
    """Return the immutable starting cash for the harness.

    Prefer the persisted ``canary_starting_equity`` so restarts rebuild
    the harness against the same baseline. Fall back to the live ledger
    during the first activation.
    """
    if state.canary_starting_equity is not None:
        return float(state.canary_starting_equity)
    if ledger is None:
        return 0.0
    if hasattr(ledger, "ensure_portfolio_state"):
        portfolio = ledger.ensure_portfolio_state()  # type: ignore[attr-defined]
        return float(getattr(portfolio, "cash", 0.0))
    return 0.0


def _reconcile_durable_orders(
    harness: PairedShadowHarness,
    state: ExperimentState,
    ledger: PortfolioLedger | object | None,
) -> None:
    """Replay durable orders missing from the shadow JSONL.

    The shadow append happens after the SQLite commit. If the process
    crashes between the two, the durable row exists but the shadow
    JSONL does not. This method backfills the missing rows so the
    harness state matches the durable ledger.

    Idempotency: the individual shadow ledgers drop duplicate
    operation_ids, so re-recording an already-applied row is a no-op.
    """
    if ledger is None:
        return
    list_rows = getattr(ledger, "list_canary_order_rows", None)
    if list_rows is None:
        return
    try:
        rows = list_rows(state.experiment_id)
    except Exception as exc:
        logger.exception(
            "Failed to read canary orders for %s during reconciliation",
            state.experiment_id,
        )
        raise RuntimeCanaryLifecycleError(
            "canary_orders_inaccessible"
        ) from exc

    for row in rows:
        order_id = str(row["id"])
        side = str(row["side"])
        ticker = str(row["ticker"])
        fill_price = float(row["fill_price"])
        fees = float(row["fees"])
        if side == "BUY":
            baseline_quantity = int(row.get("canary_baseline_quantity") or 0)
            candidate_quantity = int(row["quantity"])
            harness.record_entry(
                operation_id=order_id,
                ticker=ticker,
                baseline_quantity=baseline_quantity,
                candidate_quantity=candidate_quantity,
                fill_price=fill_price,
                fees=fees,
            )
        elif side == "SELL":
            candidate_quantity = int(row["quantity"])
            if candidate_quantity <= 0:
                continue
            harness.record_exit(
                operation_id=order_id,
                ticker=ticker,
                candidate_quantity=candidate_quantity,
                fill_price=fill_price,
                fees=fees,
            )


def _build_canary_context_with_deps(
    settings: Settings,
    ledger: PortfolioLedger | object | None,
    *,
    controller: ExperimentController | None = None,
    store: ExperimentStore | None = None,
) -> RuntimeCanaryContext | None:
    """Internal loader used by both the production API and the test seam.

    The production API only ever calls this without explicit
    ``controller`` or ``store``; the test seam allows injection so
    tests can use a tmp_path store without touching the canonical root.
    """
    canonical_root = (
        Path(settings.app.state_db_path).parent / "tuning_experiments"
    )
    if controller is not None:
        store = controller.store
    elif store is None:
        store = ExperimentStore(root=canonical_root)

    try:
        state = store.load_current()
    except Exception as exc:
        logger.exception(
            "Failed to load runtime canary state from %s", canonical_root
        )
        raise RuntimeCanaryLifecycleError(
            "canary_state_inaccessible"
        ) from exc

    if state is None:
        return None
    if state.status != "CANARY":
        return None
    if not is_runtime_canary_supported(state.change):
        logger.warning(
            "Runtime canary %s has unsupported change %s.%s; "
            "falling back to no-shadow path",
            state.experiment_id,
            state.change.section,
            state.change.field,
        )
        return None

    if controller is None:
        controller = ExperimentController(
            settings=settings,
            store=store,
            bar_loader=None,
            overrides_path=Path(settings.app.tuning_overrides_path),
        )

    artifacts_dir = _resolve_artifacts_dir(store)
    try:
        starting_cash = _resolve_starting_cash(state, ledger)
    except Exception as exc:
        logger.exception(
            "Failed to resolve starting cash for canary %s",
            state.experiment_id,
        )
        raise RuntimeCanaryLifecycleError(
            "canary_starting_cash_unavailable"
        ) from exc

    harness = PairedShadowHarness(
        artifacts_dir=artifacts_dir,
        starting_cash=starting_cash,
        change=state.change,
    )

    _reconcile_durable_orders(harness, state, ledger)

    return RuntimeCanaryContext(
        state=state,
        controller=controller,
        store=store,
        harness=harness,
        artifacts_dir=artifacts_dir,
        starting_cash=starting_cash,
    )


def begin_runtime_canary(
    settings: Settings,
    ledger: PortfolioLedger,
) -> RuntimeCanaryContext | None:
    """Begin a runtime canary context for the current cycle.

    Derives the canonical experiment root from
    ``<state_db_parent>/tuning_experiments``, builds the
    ``ExperimentController`` and ``PairedShadowHarness``, validates the
    canary state, and reconstructs paired ledgers from durable
    artifacts.

    Returns ``None`` when no experiment is in CANARY (the legacy
    no-shadow fallback).

    Raises ``RuntimeCanaryLifecycleError`` when active state is
    malformed or inaccessible — observability requires that corruption
    is not silently equated with inactivity.
    """
    return _build_canary_context_with_deps(settings, ledger)


def finish_runtime_canary(context: RuntimeCanaryContext | None) -> None:
    """Finalize the canary context: snapshot metrics and persist state.

    Captures candidate_metrics, shadow_metrics, and the completed-position
    count via the controller's snapshot. No-op when ``context`` is None
    so callers can finish without a branch on the canary state.
    """
    if context is None:
        return
    context.snapshot()


def load_runtime_canary(
    settings: Settings,
    ledger: PortfolioLedger | object | None,
    *,
    controller: ExperimentController | None = None,
    store: ExperimentStore | None = None,
    now: datetime | None = None,
) -> RuntimeCanaryContext | None:
    """Backward-compatible loader preserving the existing None-on-error contract.

    Returns ``None`` when no canary is in CANARY, when the change is
    unsupported, or when the canonical state is malformed/inaccessible.
    Production callers should use :func:`begin_runtime_canary` instead —
    this shim catches ``RuntimeCanaryLifecycleError`` so the legacy
    test suite continues to work without per-call error handling.
    """
    del now  # unused; legacy parameter
    try:
        return _build_canary_context_with_deps(
            settings,
            ledger,
            controller=controller,
            store=store,
        )
    except RuntimeCanaryLifecycleError:
        return None
