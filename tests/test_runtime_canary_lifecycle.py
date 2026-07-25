"""Tests for the runtime canary production lifecycle boundary.

The production API is ``begin_runtime_canary(settings, ledger)`` and
``finish_runtime_canary(context)``. The loader no longer accepts an
optional controller or store; the canonical experiment root is derived
from ``settings.app.state_db_path`` so the production path cannot
silently return ``None`` when dependencies are missing.

Tests use a private ``_build_canary_context_with_deps`` helper that
preserves the explicit dependency-injection seam required to drive
the harness with a custom store and controller.
"""

from __future__ import annotations

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
from trading_bot.learning.experiments.models import (
    ExperimentState,
    MetricSet,
    ParameterChange,
)
from trading_bot.learning.experiments.runtime_canary import (
    begin_runtime_canary,
    finish_runtime_canary,
)
from trading_bot.learning.experiments.store import ExperimentStore
from trading_bot.models.order import FillResult
from trading_bot.portfolio.ledger import PortfolioLedger


def _settings(db_path: Path) -> Settings:
    settings = Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )
    settings.app.state_db_path = str(db_path)
    return settings


def _seed_canary_state(
    store: ExperimentStore,
    *,
    starting_equity: float | None = 100_000.0,
) -> ExperimentState:
    state = ExperimentState(
        experiment_id="lifecycle-canary",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=True,
        canary_starting_equity=starting_equity,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)
    return state


def make_fill(
    order_id: str,
    *,
    quantity: int = 5,
    ticker: str = "AAPL",
    fill_price: float = 100.0,
    fees: float = 1.0,
) -> FillResult:
    return FillResult(
        order_id=order_id,
        ticker=ticker,
        quantity=quantity,
        fill_price=fill_price,
        fees=fees,
        filled_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _settings(tmp_path / "state.db")


@pytest.fixture
def ledger(settings: Settings) -> PortfolioLedger:
    return PortfolioLedger(Path(settings.app.state_db_path))


@pytest.fixture
def seeded_canary(settings: Settings) -> ExperimentState:
    """Seed an active canary state in the canonical store."""
    canonical_root = (
        Path(settings.app.state_db_path).parent / "tuning_experiments"
    )
    store = ExperimentStore(root=canonical_root)
    return _seed_canary_state(store)


def test_begin_derives_canonical_store(
    settings: Settings,
    ledger: PortfolioLedger,
    seeded_canary: ExperimentState,
) -> None:
    """begin_runtime_canary derives the canonical store from settings.app.state_db_path
    and does not require an injected controller or store."""
    context = begin_runtime_canary(settings, ledger)

    assert context is not None
    assert context.state.experiment_id == seeded_canary.experiment_id
    assert context.store.root == (
        Path(settings.app.state_db_path).parent / "tuning_experiments"
    )
    # The harness reconstructed from durable artifacts.
    assert context.harness is not None
    assert context.harness.starting_cash == pytest.approx(100_000.0)


def test_begin_returns_none_when_no_active_experiment(
    settings: Settings,
    ledger: PortfolioLedger,
) -> None:
    """begin_runtime_canary returns None when the canonical store is empty."""
    context = begin_runtime_canary(settings, ledger)
    assert context is None


def test_begin_returns_none_for_unsupported_change(
    settings: Settings,
    ledger: PortfolioLedger,
) -> None:
    """begin_runtime_canary returns None for an unsupported parameter change."""
    canonical_root = (
        Path(settings.app.state_db_path).parent / "tuning_experiments"
    )
    store = ExperimentStore(root=canonical_root)
    state = ExperimentState(
        experiment_id="unsupported",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="support_threshold",
            baseline=0.55,
            candidate=0.50,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=True,
        canary_starting_equity=10_000.0,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)

    context = begin_runtime_canary(settings, ledger)
    assert context is None


def test_begin_returns_none_for_non_canary_state(
    settings: Settings,
    ledger: PortfolioLedger,
) -> None:
    """begin_runtime_canary returns None for non-CANARY statuses so callers
    fall through to the legacy no-shadow path."""
    canonical_root = (
        Path(settings.app.state_db_path).parent / "tuning_experiments"
    )
    store = ExperimentStore(root=canonical_root)
    state = ExperimentState(
        experiment_id="proposed",
        status="PROPOSED",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        canary_starting_equity=10_000.0,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)

    context = begin_runtime_canary(settings, ledger)
    assert context is None


def test_begin_reconciles_missing_shadow_fill(
    settings: Settings,
    ledger: PortfolioLedger,
    seeded_canary: ExperimentState,
) -> None:
    """begin_runtime_canary replays durable BUY rows missing from JSONL so a
    crash between SQLite commit and shadow append does not lose the paired
    fill."""
    ledger.record_fill(
        make_fill("buy-1"),
        "BUY",
        canary_experiment_id=seeded_canary.experiment_id,
        canary_baseline_quantity=10,
    )

    context = begin_runtime_canary(settings, ledger)

    assert context is not None
    # Candidate ledger: 5 shares (the durable BUY quantity).
    assert context.harness.candidate.snapshot_positions()["AAPL"]["qty"] == 5
    # Baseline ledger: 10 shares (the persisted baseline quantity).
    assert context.harness.baseline.snapshot_positions()["AAPL"]["qty"] == 10


def test_begin_reconciles_missing_shadow_exit(
    settings: Settings,
    ledger: PortfolioLedger,
    seeded_canary: ExperimentState,
) -> None:
    """begin_runtime_canary replays durable SELL rows missing from JSONL,
    driving the proportional baseline exit."""
    ledger.record_fill(
        make_fill("buy-1", quantity=5),
        "BUY",
        canary_experiment_id=seeded_canary.experiment_id,
        canary_baseline_quantity=10,
    )
    ledger.record_fill(
        make_fill("sell-1",
                  quantity=2,
                  fill_price=110.0),
        "SELL",
        canary_experiment_id=seeded_canary.experiment_id,
    )

    context = begin_runtime_canary(settings, ledger)

    assert context is not None
    # Candidate sold 2 of 5 → 3 remaining.
    assert context.harness.candidate.snapshot_positions()["AAPL"]["qty"] == 3
    # Baseline sold 2/5 × 10 = 4 → 6 remaining.
    assert context.harness.baseline.snapshot_positions()["AAPL"]["qty"] == 6


def test_begin_idempotent_against_existing_shadow_state(
    settings: Settings,
    ledger: PortfolioLedger,
    seeded_canary: ExperimentState,
) -> None:
    """A second begin_runtime_canary call on the same experiment must not
    double-record durable fills that were already mirrored to the shadow."""
    ledger.record_fill(
        make_fill("buy-1"),
        "BUY",
        canary_experiment_id=seeded_canary.experiment_id,
        canary_baseline_quantity=10,
    )

    first = begin_runtime_canary(settings, ledger)
    assert first is not None
    # Replay the same fill into the shadow via record_entry (the live
    # trading path) so a real restart scenario has both sides present.
    first.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )

    Second = begin_runtime_canary(settings, ledger)
    assert Second is not None
    # Idempotent: candidate is still 5 shares, not 10.
    assert Second.harness.candidate.snapshot_positions()["AAPL"]["qty"] == 5
    assert Second.harness.baseline.snapshot_positions()["AAPL"]["qty"] == 10


def test_finish_snapshots_metrics_and_completed_position_count(
    settings: Settings,
    ledger: PortfolioLedger,
    seeded_canary: ExperimentState,
) -> None:
    """finish_runtime_canary persists candidate_metrics, shadow_metrics,
    and the completed-position count via the controller snapshot."""
    context = begin_runtime_canary(settings, ledger)
    assert context is not None

    context.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    context.record_exit(
        operation_id="sell-1",
        ticker="AAPL",
        candidate_quantity=5,
        fill_price=110.0,
        fees=1.0,
    )

    finish_runtime_canary(context)

    reloaded = context.store.load_current()
    assert reloaded is not None
    assert reloaded.candidate_metrics is not None
    assert reloaded.candidate_metrics.trades == 1
    assert reloaded.shadow_metrics is not None
    assert reloaded.shadow_metrics.trades == 1
    assert reloaded.canary_closed_trades == 1


def test_finish_noop_for_none_context() -> None:
    """finish_runtime_canary(None) is a no-op so callers can finish without
    a branch on the canary state."""
    finish_runtime_canary(None)
