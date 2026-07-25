"""Runtime canary context: bridges live trading paths to the paired shadow harness.

A ``RuntimeCanaryContext`` owns the live view of an experiment in
``CANARY``: the loaded ``ExperimentState``, the ``ExperimentController``
for marking metrics and decisions, and the paired ``PairedShadowHarness``
that mirrors every BUY and SELL into two parallel ledgers.

The seam is constructed once per CLI invocation (or once per
continuous-loop cycle). When no experiment is in CANARY, or when the
runtime support guard rejects the change, ``load_runtime_canary`` returns
``None`` so the trading paths transparently fall back to the legacy
no-shadow behavior.

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


def load_runtime_canary(
    settings: Settings,
    ledger: PortfolioLedger | object | None,
    *,
    controller: ExperimentController | None = None,
    store: ExperimentStore | None = None,
    now: datetime | None = None,
) -> RuntimeCanaryContext | None:
    """Return a context for the active CANARY experiment, or ``None``.

    Returns ``None`` when:
        - No experiment is in CANARY.
        - The candidate change is unsupported by the harness (anything
          other than the allowlisted sizing-only parameter).
        - The persistence seams are unreachable.

    The caller treats a ``None`` return as "no shadow tracking this
    cycle" and proceeds with the legacy no-shadow path.
    """
    if controller is not None:
        ctl = controller
        store = controller.store
    elif store is not None:
        ctl = ExperimentController(
            settings=settings,
            store=store,
            bar_loader=None,
            overrides_path=Path(settings.app.tuning_overrides_path),
        )
    else:
        return None

    try:
        state = store.load_current()
    except Exception:  # noqa: BLE001
        return None

    if state is None or state.status != "CANARY":
        return None

    if not is_runtime_canary_supported(state.change):
        return None

    artifacts_dir = _resolve_artifacts_dir(store)
    try:
        starting_cash = _resolve_starting_cash(state, ledger)
    except Exception:  # noqa: BLE001
        return None

    harness = PairedShadowHarness(
        artifacts_dir=artifacts_dir,
        starting_cash=starting_cash,
        change=state.change,
    )
    return RuntimeCanaryContext(
        state=state,
        controller=ctl,
        store=store,
        harness=harness,
        artifacts_dir=artifacts_dir,
        starting_cash=starting_cash,
    )


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
