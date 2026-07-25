"""Tests for fail-closed activation and unified terminal finalization.

Task 4 delivers:
1. Activate canary BEFORE candidate bytes are activated.
2. _live_portfolio_is_flat fails closed (returns False on read errors).
3. finalize_terminal owns status, restoration, archival, and event logging.
4. Unsupported, non-flat, and runtime invalidation routes all converge
   through finalize_terminal so a subsequent proposal can run.
5. RuntimeCanaryContext.invalidate delegates to finalize_terminal.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from trading_bot.config.settings import (
    PaperSettings,
    Settings,
    StrategySettings,
    StrategyTrackerSettings,
    SupermodelSettings,
)
from trading_bot.learning.experiments.controller import ExperimentController
from trading_bot.learning.experiments.models import (
    ExperimentState,
    MetricSet,
    ParameterChange,
)
from trading_bot.learning.experiments.runtime_canary import (
    RuntimeCanaryContext,
    RuntimeCanaryLifecycleError,
    load_runtime_canary,
)
from trading_bot.learning.experiments.store import ExperimentStore
from trading_bot.models.portfolio import Position, PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger


def _settings(state_db_path: Path) -> Settings:
    settings = Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )
    settings.app.state_db_path = str(state_db_path)
    return settings


def _seed_canary_state(
    store: ExperimentStore,
    *,
    experiment_id: str = "activation-test",
    supported: bool = True,
    candidate: float = 0.5,
    status: str = "CANARY",
    armed: bool = True,
) -> ExperimentState:
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier"
        if supported
        else "support_threshold",
        baseline=1.0,
        candidate=candidate,
    )
    state = ExperimentState(
        experiment_id=experiment_id,
        status=status,
        change=change,
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=armed,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)
    return state


def _accepted_offline() -> "OfflineEvaluation":
    from trading_bot.learning.experiments.replay import OfflineEvaluation

    return OfflineEvaluation(
        accepted=True,
        reasons=[],
        baseline_train=MetricSet(
            trades=30, profit_factor=1.10, net_pnl=50.0, max_drawdown_pct=2.0
        ),
        candidate_train=MetricSet(
            trades=30, profit_factor=1.15, net_pnl=80.0, max_drawdown_pct=1.8
        ),
        baseline_validation=MetricSet(
            trades=20, profit_factor=1.10, net_pnl=80.0, max_drawdown_pct=2.0
        ),
        candidate_validation=MetricSet(
            trades=20, profit_factor=1.15, net_pnl=120.0, max_drawdown_pct=1.8
        ),
    )


def _read_events(store: ExperimentStore) -> list[dict]:
    if not store.events_path.exists():
        return []
    lines = store.events_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _seed_proposed_state(
    store: ExperimentStore,
    *,
    experiment_id: str,
    change: ParameterChange,
    baseline_bytes: bytes,
) -> ExperimentState:
    """Seed a PROPOSED state with drift-safe checksum references."""
    overrides_snapshot = {
        change.section: {change.field: change.candidate},
    }
    store.snapshot_overrides(
        experiment_id, "candidate", overrides_snapshot
    )
    candidate_path = store.root / experiment_id / "candidate.yaml"
    state = ExperimentState(
        experiment_id=experiment_id,
        status="PROPOSED",
        change=change,
        started_at=datetime.now(timezone.utc),
    )
    state.candidate_checksum = store.checksum(candidate_path)
    store.snapshot_overrides_bytes(
        experiment_id, "baseline", baseline_bytes
    )
    state.baseline_checksum = store.checksum(
        store.root / experiment_id / "baseline.yaml"
    )
    state.baseline_was_absent = False
    store.save_current(state)
    return state


def test_activation_persists_equity_before_candidate_write(tmp_path: Path) -> None:
    """The canary_starting_equity_recorded event must appear BEFORE
    canary_started in events.jsonl so restarts can replay the immutable
    baseline cash before any candidate bytes are written to the live
    overrides."""
    settings = _settings(tmp_path / "state.db")
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")
    overrides = tmp_path / "overrides.yaml"
    baseline_bytes = b"supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n"
    overrides.write_bytes(baseline_bytes)

    state_db = Path(settings.app.state_db_path)
    PortfolioLedger(state_db)  # ensure flat portfolio

    store = ExperimentStore(root=tmp_path / "experiments")
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )
    _seed_proposed_state(
        store,
        experiment_id="activation-order",
        change=change,
        baseline_bytes=baseline_bytes,
    )

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )
    controller._run_offline = lambda s: _accepted_offline()  # type: ignore[method-assign]

    decided = controller.evaluate()
    assert decided is not None
    assert decided.status == "CANARY"

    events = _read_events(store)
    event_names = [e["event"] for e in events]
    assert "canary_starting_equity_recorded" in event_names
    assert "canary_started" in event_names
    assert (
        event_names.index("canary_starting_equity_recorded")
        < event_names.index("canary_started")
    ), (
        "canary_starting_equity_recorded must precede canary_started; "
        f"events={event_names}"
    )


def test_non_flat_portfolio_blocks_activation(tmp_path: Path) -> None:
    """When the live portfolio is not flat at activation, the experiment
    is INCONCLUSIVE and the candidate bytes are NOT written to overrides."""
    settings = _settings(tmp_path / "state.db")
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")
    overrides = tmp_path / "overrides.yaml"
    baseline_bytes = (
        b"supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n"
    )
    overrides.write_bytes(baseline_bytes)

    state_db = Path(settings.app.state_db_path)
    ledger = PortfolioLedger(state_db)
    pos = Position(
        ticker="AAPL",
        quantity=5,
        average_cost=100.0,
        stop_loss=99.0,
        profit_target=101.0,
        entry_fees=1.0,
    )
    ledger.save_portfolio_state(
        PortfolioState(
            cash=50_000.0,
            equity=50_500.0,
            positions={"AAPL": pos},
            realized_pnl=0.0,
            unrealized_pnl=500.0,
        )
    )

    store = ExperimentStore(root=tmp_path / "experiments")
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )
    _seed_proposed_state(
        store,
        experiment_id="nonflat-activation",
        change=change,
        baseline_bytes=baseline_bytes,
    )

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )
    controller._run_offline = lambda s: _accepted_offline()  # type: ignore[method-assign]

    decided = controller.evaluate()
    assert decided is not None
    assert decided.status == "INCONCLUSIVE"
    assert decided.last_error is not None
    assert "non_flat_portfolio_on_canary_start" in decided.last_error

    # The overrides file must remain unchanged (baseline bytes).
    assert overrides.read_bytes() == baseline_bytes


def test_ledger_read_error_is_not_flat(tmp_path: Path) -> None:
    """A corrupted/inaccessible ledger must fail the gate (return False
    from _live_portfolio_is_flat)."""
    # Use a directory as the state_db_path so sqlite3.connect fails.
    state_db = tmp_path / "state.db-dir"
    state_db.mkdir()

    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    controller = ExperimentController(
        settings=settings,
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    # The state_db_path exists (it's a directory), so we proceed past the
    # "never initialized" fast-path. The connect call then fails.
    assert controller._live_portfolio_is_flat() is False


def test_never_initialized_ledger_is_flat(tmp_path: Path) -> None:
    """A never-initialized ledger (no SQLite file) is flat.

    This is the companion to test_ledger_read_error_is_not_flat: the
    missing-file path must still mean flat so the first activation
    against a fresh DB is allowed.
    """
    state_db = tmp_path / "state.db"
    assert not state_db.exists()

    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    controller = ExperimentController(
        settings=settings,
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    assert controller._live_portfolio_is_flat() is True


def test_unsupported_change_archived_inconclusive(tmp_path: Path) -> None:
    """An unsupported parameter change is archived as INCONCLUSIVE so the
    next proposal can run."""
    settings = _settings(tmp_path / "state.db")
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")
    overrides = tmp_path / "overrides.yaml"
    baseline_bytes = b"supermodel:\n  support_threshold: 0.55\n"
    overrides.write_bytes(baseline_bytes)

    state_db = Path(settings.app.state_db_path)
    PortfolioLedger(state_db)  # ensure flat

    store = ExperimentStore(root=tmp_path / "experiments")
    change = ParameterChange(
        section="supermodel",
        field="support_threshold",
        baseline=0.55,
        candidate=0.50,
    )
    _seed_proposed_state(
        store,
        experiment_id="unsupported-archive",
        change=change,
        baseline_bytes=baseline_bytes,
    )

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )
    controller._run_offline = lambda s: _accepted_offline()  # type: ignore[method-assign]

    decided = controller.evaluate()
    assert decided is not None
    assert decided.status == "INCONCLUSIVE"
    assert decided.last_error is not None
    assert "unsupported_runtime_canary" in decided.last_error

    # The state must be archived so the next proposal can run.
    assert store.load_current() is None
    archived_path = (
        store.root / "archived" / "unsupported-archive" / "current.json"
    )
    assert archived_path.exists()
    archived = ExperimentState.model_validate_json(archived_path.read_text())
    assert archived.status == "INCONCLUSIVE"


def test_runtime_invalidation_restores_and_archives(tmp_path: Path) -> None:
    """RuntimeCanaryContext.invalidate restores baseline, archives state,
    and persists the reason."""
    settings = _settings(tmp_path / "state.db")
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")
    overrides = tmp_path / "overrides.yaml"
    baseline_bytes = (
        b"supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n"
    )
    overrides.write_bytes(baseline_bytes)

    state_db = Path(settings.app.state_db_path)
    PortfolioLedger(state_db)

    store = ExperimentStore(root=tmp_path / "experiments")
    _seed_canary_state(store, experiment_id="rt-invalidate")

    # Snapshot baseline so restore_baseline_exact succeeds.
    store.snapshot_overrides_bytes(
        "rt-invalidate", "baseline", baseline_bytes
    )
    # Snapshot candidate bytes so activate_candidate has something to write.
    candidate_bytes = b"supermodel:\n  range_bound_trend_caution_multiplier: 0.5\n"
    store.snapshot_overrides_bytes(
        "rt-invalidate", "candidate", candidate_bytes
    )

    # Write candidate bytes to overrides (simulating activation).
    store.activate_candidate("rt-invalidate", overrides)
    assert overrides.read_bytes() == candidate_bytes
    assert overrides.read_bytes() != baseline_bytes

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )

    class _FakeLedger:
        def ensure_portfolio_state(self):
            return PortfolioState(
                cash=100_000.0,
                equity=100_000.0,
                positions={},
                realized_pnl=0.0,
                unrealized_pnl=0.0,
            )

    ctx = load_runtime_canary(
        settings, _FakeLedger(), controller=controller  # type: ignore[arg-type]
    )
    assert ctx is not None
    assert isinstance(ctx, RuntimeCanaryContext)

    ctx.invalidate("mock_persistence_failure")

    # The overrides file must be restored to baseline bytes.
    assert overrides.read_bytes() == baseline_bytes

    # The state must be archived: current.json is gone, the archived
    # directory holds the terminal state.
    assert store.load_current() is None
    archived_path = (
        store.root / "archived" / "rt-invalidate" / "current.json"
    )
    assert archived_path.exists()
    archived = ExperimentState.model_validate_json(archived_path.read_text())
    assert archived.status == "INCONCLUSIVE"
    assert archived.last_error == "mock_persistence_failure"


def test_finalize_terminal_restoration_failure_raises(tmp_path: Path) -> None:
    """When restore_baseline_exact fails, finalize_terminal raises
    RuntimeCanaryLifecycleError and does NOT archive the state."""
    settings = _settings(tmp_path / "state.db")
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n",
        encoding="utf-8",
    )

    store = ExperimentStore(root=tmp_path / "experiments")
    state = ExperimentState(
        experiment_id="no-baseline",
        status="PROPOSED",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
    )
    store.save_current(state)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )

    # No baseline.yaml or baseline.absent snapshot exists, so
    # restore_baseline_exact returns False. finalize_terminal must
    # raise and skip archival.
    with pytest.raises(RuntimeCanaryLifecycleError):
        controller.finalize_terminal(
            state, "INCONCLUSIVE", reason="restoration_failed"
        )

    # The state was NOT archived: current.json is still the original
    # PROPOSED state.
    reloaded = store.load_current()
    assert reloaded is not None
    assert reloaded.status == "PROPOSED"
    assert (
        store.root / "archived" / "no-baseline" / "current.json"
    ).exists() is False
