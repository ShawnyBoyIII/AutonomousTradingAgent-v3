"""Tests for wiring RuntimeCanaryContext through CLI entry points and the
continuous loop. The context is loaded once per cycle, threaded into the
BUY and SELL execution paths, and snapshotted at the end of each phase.

These tests verify the seam exists and is **opt-in**: passing nothing
preserves legacy behavior.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

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
from trading_bot.learning.experiments.store import ExperimentStore


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
    with_metrics: bool = False,
) -> ExperimentState:
    state = ExperimentState(
        experiment_id="cli-wiring",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=True,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    if with_metrics:
        state.candidate_metrics = MetricSet(
            trades=5,
            profit_factor=1.15,
            net_pnl=80.0,
            max_drawdown_pct=1.0,
        )
        state.shadow_metrics = MetricSet(
            trades=5,
            profit_factor=1.05,
            net_pnl=60.0,
            max_drawdown_pct=1.2,
        )
    store.save_current(state)
    return state


def test_load_runtime_canary_returns_context_for_supported_canary(
    tmp_path: Path,
) -> None:
    """When an experiment is in CANARY with a supported change,
    load_runtime_canary returns a context with a harness.
    """
    from trading_bot.learning.experiments.runtime_canary import (
        load_runtime_canary,
    )
    from trading_bot.portfolio.ledger import PortfolioLedger

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    store = ExperimentStore(root=tmp_path / "experiments")
    _seed_canary_state(store)
    ledger = PortfolioLedger(state_db)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    ctx = load_runtime_canary(settings, ledger, controller=controller)
    assert ctx is not None
    assert ctx.harness is not None


def test_load_returns_none_for_unsupported_experiment(tmp_path: Path) -> None:
    """An experiment with an unsupported parameter returns None."""
    from datetime import datetime, timezone

    from trading_bot.learning.experiments.runtime_canary import (
        load_runtime_canary,
    )
    from trading_bot.portfolio.ledger import PortfolioLedger

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)

    store = ExperimentStore(root=tmp_path / "experiments")
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
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)
    ledger = PortfolioLedger(state_db)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    ctx = load_runtime_canary(settings, ledger, controller=controller)
    assert ctx is None


def test_load_returns_none_for_non_canary_state(tmp_path: Path) -> None:
    """Experiments not in CANARY (e.g. PROPOSED, OFFLINE_REJECTED) return
    None so callers fall through to the legacy no-shadow behavior.
    """
    from datetime import datetime, timezone

    from trading_bot.learning.experiments.runtime_canary import (
        load_runtime_canary,
    )
    from trading_bot.portfolio.ledger import PortfolioLedger

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)

    store = ExperimentStore(root=tmp_path / "experiments")
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
        runtime_canary_armed=True,
        baseline_metrics=MetricSet(
            trades=24,
            profit_factor=1.20,
            net_pnl=320.0,
            max_drawdown_pct=2.0,
        ),
    )
    store.save_current(state)
    ledger = PortfolioLedger(state_db)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    ctx = load_runtime_canary(settings, ledger, controller=controller)
    assert ctx is None


def test_context_persists_metrics_on_snapshot(tmp_path: Path) -> None:
    """After record_entry/record_exit, ctx.snapshot() persists the runtime
    metrics into the experiment state so the next process sees the
    updated trade count.
    """
    from trading_bot.learning.experiments.runtime_canary import (
        load_runtime_canary,
    )
    from trading_bot.portfolio.ledger import PortfolioLedger

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    store = ExperimentStore(root=tmp_path / "experiments")
    _seed_canary_state(store)
    ledger = PortfolioLedger(state_db)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    ctx = load_runtime_canary(settings, ledger, controller=controller)
    assert ctx is not None

    ctx.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=1,
        candidate_quantity=1,
        fill_price=100.0,
        fees=1.0,
    )
    ctx.record_exit(
        operation_id="sell-1",
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


def test_cli_paper_trade_accepts_runtime_canary_kwarg_only(
    tmp_path: Path,
) -> None:
    """The paper-trade CLI command accepts runtime_canary keyword-only."""
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)

    # Verify acceptance of the keyword. The empty-symbol path goes
    # through the kill switch early.
    import inspect

    sig = inspect.signature(run_paper_trade)
    assert "runtime_canary" in sig.parameters


def test_continuous_loop_manage_positions_accepts_runtime_canary(
    tmp_path: Path,
) -> None:
    """The continuous-loop _run_manage_positions_once accepts the kwarg."""
    from trading_bot.runtime.continuous_loop import _run_manage_positions_once

    import inspect

    sig = inspect.signature(_run_manage_positions_once)
    assert "runtime_canary" in sig.parameters


def test_cli_paper_trade_dry_run_path_doesnt_touch_harness(
    tmp_path: Path,
) -> None:
    """Sanity: dry-run never records into the harness even when the
    context is supplied.
    """
    from datetime import datetime, timezone

    import yaml as yaml_mod

    from trading_bot.learning.experiments.runtime_canary import (
        RuntimeCanaryContext,
    )
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        yaml_mod.safe_dump({"supermodel": {"range_bound_trend_caution_multiplier": 1.0}}),
        encoding="utf-8",
    )

    class _Harness:
        def __init__(self) -> None:
            self.entries: list[dict[str, Any]] = []
            self.exits: list[dict[str, Any]] = []

        def record_entry(self, **kwargs) -> None:
            self.entries.append(kwargs)

        def record_exit(self, **kwargs) -> None:
            self.exits.append(kwargs)

        def candidate_metrics(self):
            return MetricSet(
                trades=0,
                profit_factor=0.0,
                net_pnl=0.0,
                max_drawdown_pct=0.0,
            )

        def baseline_metrics(self):
            return MetricSet(
                trades=0,
                profit_factor=0.0,
                net_pnl=0.0,
                max_drawdown_pct=0.0,
            )

        def closed_trade_counts_match(self) -> bool:
            return True

    store = ExperimentStore(root=tmp_path / "experiments")
    state = _seed_canary_state(store)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )

    harness = _Harness()
    ctx = RuntimeCanaryContext(
        state=state,
        controller=controller,
        store=store,
        harness=harness,
        artifacts_dir=tmp_path / "experiments" / "cli-wiring",
        starting_cash=10_000.0,
    )

    run_paper_trade([], settings, dry_run=True, runtime_canary=ctx)
    assert harness.entries == []
    assert harness.exits == []
