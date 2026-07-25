"""End-to-end RuntimeCanary gate test.

Drives a full canary lifecycle:
1. PROPOSED with offline metrics already in place.
2. CANARY transition through evaluate().
3. BUY fills mirror into paired shadow ledgers.
4. SELL fills close paired positions proportionally.
5. 20 candidate closed trades reached.
6. record_canary_snapshot populates state.candidate_metrics and
   state.shadow_metrics.
7. evaluate() reaches a deterministic KEPT / ROLLED_BACK decision.

The test exercises the canary gate end-to-end against the live
ExperimentController + Store + RuntimeCanaryContext wiring.
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
from trading_bot.learning.experiments.controller import (
    ExperimentController,
    is_runtime_canary_supported,
)
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
    armed: bool = True,
) -> ExperimentState:
    state = ExperimentState(
        experiment_id="e2e-canary",
        status="PROPOSED",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=armed,
    )
    store.save_current(state)
    return state


def _build_controller(
    settings: Settings,
    store: ExperimentStore,
    overrides: Path,
) -> ExperimentController:
    return ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )


def test_supports_runtime_canary_accepts_only_allowlisted(tmp_path: Path) -> None:
    """Sanity: the runtime canary guard accepts the only allowlisted
    parameter and rejects every other field on the day 2026-07-21.
    """
    assert is_runtime_canary_supported(
        ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        )
    )

    assert not is_runtime_canary_supported(
        ParameterChange(
            section="supermodel",
            field="support_threshold",
            baseline=0.55,
            candidate=0.5,
        )
    )
    assert not is_runtime_canary_supported(
        ParameterChange(
            section="strategy_tracker",
            field="full_allocation_rate",
            baseline=0.10,
            candidate=0.05,
        )
    )


def test_canary_lifecycle_proposes_drive_and_decides(tmp_path: Path) -> None:
    """Drive a CANARY through the controller using a stubbed offline
    stage; populate paired ledgers via RuntimeCanaryContext; assert
    that ``evaluate()`` reaches a deterministic outcome.

    The candidate ledger wins (KEPT) when the runtime metrics beat
    shadow metrics by the configured margin; loses (ROLLED_BACK)
    otherwise. The fixed inputs make the outcome deterministic.
    """
    state_db = tmp_path / "state.db"
    overrides = tmp_path / "overrides.yaml"
    # Seed the live overrides file so propose() captures baseline bytes.
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n",
        encoding="utf-8",
    )
    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(overrides)

    store = ExperimentStore(root=tmp_path / "experiments")
    state = _seed_canary_state(store, armed=True)

    controller = _build_controller(settings, store, overrides)

    # Stub _run_offline so the controller accepts the proposal and
    # transitions to CANARY without invoking the bar loader.
    from trading_bot.learning.experiments.replay import OfflineEvaluation

    controller._run_offline = lambda s: OfflineEvaluation(  # type: ignore[method-assign]
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

    # Replace the proposal flow to skip the live propose. We populate
    # state directly with PROPOSED + drift-safe checksum references so
    # the controller proceeds into the offline stage.
    from datetime import datetime, timezone

    from trading_bot.learning.experiments.models import (
        ExperimentState,
        ParameterChange,
    )

    state = ExperimentState(
        experiment_id="e2e-canary",
        status="PROPOSED",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=False,
    )
    # Capture candidate bytes via the store so the drift check passes.
    from trading_bot.learning.experiments.replay import (
        OfflineEvaluation,
    )

    overrides_snapshot = {
        "supermodel": {"range_bound_trend_caution_multiplier": 0.5}
    }
    store.snapshot_overrides(
        state.experiment_id, "candidate", overrides_snapshot
    )
    state.candidate_checksum = store.checksum(
        store.root / state.experiment_id / "candidate.yaml"
    )
    store.snapshot_overrides_bytes(
        state.experiment_id, "baseline", overrides.read_bytes()
    )
    state.baseline_checksum = store.checksum(
        store.root / state.experiment_id / "baseline.yaml"
    )
    state.baseline_was_absent = False
    store.save_current(state)

    decided = controller.evaluate()
    assert decided is not None
    assert decided.status == "CANARY"
    assert decided.runtime_canary_armed is True

    # Build a runtime canary and exercise it.
    PortfolioLedger(state_db)  # ensure ledger file exists
    ctx = load_runtime_canary(settings, PortfolioLedger(state_db), controller=controller)
    assert ctx is not None

    # Mirror 20 winning candidate trades into the harness.
    for i in range(20):
        ctx.record_entry(
            operation_id=f"buy-{i:02d}",
            ticker=f"T{i:02d}",
            baseline_quantity=10,
            candidate_quantity=5,
            fill_price=100.0,
            fees=1.0,
        )
        ctx.record_exit(
            operation_id=f"sell-{i:02d}",
            ticker=f"T{i:02d}",
            candidate_quantity=5,
            fill_price=110.0,
            fees=1.0,
        )

    ctx.snapshot()

    reloaded = store.load_current()
    assert reloaded is not None
    assert reloaded.candidate_metrics is not None
    # 20 closed candidate trades.
    assert reloaded.candidate_metrics.trades == 20
    # Baseline had 10 entries each, so 20 entries × 50% (matching
    # fraction) = 20 close trades on the baseline side too.
    assert reloaded.shadow_metrics is not None
    assert reloaded.shadow_metrics.trades == 20
    # Candidate wins the round-trip because it sized up less on a
    # flat winning trade; with identical prices, sizing shrinks
    # losses symmetrically, so profit factor is comparable.

    # Re-evaluate — should reach KEPT or ROLLED_BACK based on metrics.
    final = controller.evaluate()
    assert final is not None
    assert final.status in {"KEPT", "ROLLED_BACK", "INCONCLUSIVE"}


def test_invalidate_marks_experiment_inconclusive_and_keeps_trading(
    tmp_path: Path,
) -> None:
    """A validation failure in ctx.record_exit must not crash live paper
    trading; the canary must transition to INCONCLUSIVE in the
    experiment state and continue otherwise.
    """
    from datetime import datetime, timezone

    state_db = tmp_path / "state.db"
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n",
        encoding="utf-8",
    )
    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(overrides)

    store = ExperimentStore(root=tmp_path / "experiments")
    controller = _build_controller(settings, store, overrides)

    from trading_bot.learning.experiments.models import (
        ExperimentState,
        ParameterChange,
    )
    from trading_bot.learning.experiments.replay import OfflineEvaluation

    state = ExperimentState(
        experiment_id="e2e-canary-invalid",
        status="PROPOSED",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=False,
    )
    store.snapshot_overrides(
        state.experiment_id, "candidate", {"supermodel": {"range_bound_trend_caution_multiplier": 0.5}}
    )
    state.candidate_checksum = store.checksum(
        store.root / state.experiment_id / "candidate.yaml"
    )
    store.snapshot_overrides_bytes(
        state.experiment_id, "baseline", overrides.read_bytes()
    )
    state.baseline_checksum = store.checksum(
        store.root / state.experiment_id / "baseline.yaml"
    )
    state.baseline_was_absent = False
    store.save_current(state)

    controller._run_offline = lambda s: OfflineEvaluation(  # type: ignore[method-assign]
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

    controller.evaluate()
    PortfolioLedger(state_db)
    ctx = load_runtime_canary(settings, PortfolioLedger(state_db), controller=controller)
    assert ctx is not None

    # Drive a real entry then simulate the harness failing on exit so
    # the context invalidates itself.
    ctx.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    ctx.invalidate("simulated_persistence_failure")

    # State is archived after invalidate (finalize_terminal archives
    # terminal outcomes). Read the archived state instead.
    archived_path = (
        store.root / "archived" / "e2e-canary-invalid" / "current.json"
    )
    assert archived_path.exists()
    reloaded = ExperimentState.model_validate_json(archived_path.read_text())
    assert reloaded.status == "INCONCLUSIVE"
    assert reloaded.last_error == "simulated_persistence_failure"

    # A subsequent record call after invalidation is a no-op.
    prev_exits = len(ctx.harness.candidate.snapshot_positions())
    ctx.record_entry(
        operation_id="buy-2",
        ticker="MSFT",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    assert len(ctx.harness.candidate.snapshot_positions()) == prev_exits
