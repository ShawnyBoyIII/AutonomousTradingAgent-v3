"""Tests for the RuntimeCanaryContext seam that exposes the paired shadow
harness to the runtime trading paths.

The context is constructed once per CLI invocation or per continuous-loop
cycle. It owns the experiment state, controller, harness, and persistence
location. When no experiment is in CANARY, or when the supported
parameter guard rejects the change, ``load_runtime_canary`` returns
``None`` so the runtime paths fall back to the legacy no-shadow
behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

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
    load_runtime_canary,
)
from trading_bot.learning.experiments.store import ExperimentStore


def _settings() -> Settings:
    return Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )


def _seed_canary_state(
    store: ExperimentStore,
    *,
    candidate: float = 0.5,
    supported: bool = True,
) -> ExperimentState:
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier"
        if supported
        else "support_threshold",
        baseline=1.0,
        candidate=candidate if supported else 0.5,
    )
    state = ExperimentState(
        experiment_id=f"rt-ctx-{supported}",
        status="CANARY",
        change=change,
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=True,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)
    return state


class _FakeLedger:
    """Minimal duck-typed ledger for the seam's portfolio/equity queries."""

    def __init__(self, equity: float = 100_000.0) -> None:
        self._equity = equity

    def ensure_portfolio_state(self):
        from trading_bot.models.portfolio import PortfolioState

        return PortfolioState(
            cash=self._equity,
            equity=self._equity,
            positions={},
            realized_pnl=0.0,
            unrealized_pnl=0.0,
        )


def test_load_returns_none_without_active_experiment(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    result = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]

    assert result is None


def test_load_returns_none_for_unsupported_change(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    _seed_canary_state(store, supported=False)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    result = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]

    assert result is None


def test_load_returns_context_for_supported_canary(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    _seed_canary_state(store, candidate=0.5)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    result = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]

    assert isinstance(result, RuntimeCanaryContext)
    assert result.harness is not None
    assert result.state.experiment_id == "rt-ctx-True"
    # Starting equity reads from the ledger.
    assert result.starting_cash == 100_000.0


def test_record_entry_mirrors_into_paired_harness(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    _seed_canary_state(store)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    ctx = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]
    assert ctx is not None

    ctx.record_entry(
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )

    positions = ctx.harness.baseline.snapshot_positions()
    assert positions["AAPL"]["qty"] == 10
    positions = ctx.harness.candidate.snapshot_positions()
    assert positions["AAPL"]["qty"] == 5


def test_record_exit_drives_proportional_baseline_exit(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    _seed_canary_state(store)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    ctx = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]
    assert ctx is not None

    ctx.record_entry(
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    ctx.record_exit(
        ticker="AAPL",
        candidate_quantity=2,
        fill_price=110.0,
        fees=1.0,
    )

    # Candidate 2/5 = 40% sold → baseline 40% × 10 = 4.
    assert ctx.harness.baseline.snapshot_positions()["AAPL"]["qty"] == 6
    assert ctx.harness.candidate.snapshot_positions()["AAPL"]["qty"] == 3


def test_snapshot_persists_runtime_metrics(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    _seed_canary_state(store)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    ctx = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]
    assert ctx is not None

    ctx.record_entry(
        ticker="AAPL",
        baseline_quantity=1,
        candidate_quantity=1,
        fill_price=100.0,
        fees=1.0,
    )
    ctx.record_exit(
        ticker="AAPL",
        candidate_quantity=1,
        fill_price=110.0,
        fees=1.0,
    )
    ctx.snapshot()

    loaded = store.load_current()
    assert loaded is not None
    assert loaded.candidate_metrics is not None
    assert loaded.candidate_metrics.trades == 1
    assert loaded.shadow_metrics is not None
    assert loaded.shadow_metrics.trades == 1


def test_invalidate_marks_canary_inconclusive(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    state = _seed_canary_state(store)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    ctx = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]
    assert ctx is not None

    ctx.invalidate("mock_persistence_failure")

    loaded = store.load_current()
    assert loaded is not None
    assert loaded.status == "INCONCLUSIVE"
    assert loaded.last_error == "mock_persistence_failure"
    assert state.experiment_id == loaded.experiment_id


def test_record_entry_records_market_session(tmp_path: Path) -> None:
    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")
    store = ExperimentStore(root=tmp_path / "experiments")

    _seed_canary_state(store)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    ledger = _FakeLedger()

    ctx = load_runtime_canary(settings, ledger, controller=controller)  # type: ignore[arg-type]
    assert ctx is not None

    ctx.record_entry(
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
        session_date="2026-07-21",
    )

    loaded = store.load_current()
    assert loaded is not None
    assert "2026-07-21" in loaded.market_sessions
