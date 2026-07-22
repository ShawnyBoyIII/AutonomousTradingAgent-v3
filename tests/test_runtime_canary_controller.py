"""Tests for the runtime canary lifecycle guards on ExperimentController.

The allowed runtime canary parameter is the sizing-only
``supermodel.range_bound_trend_caution_multiplier``. Anything else forces
INCONCLUSIVE so we never silently run a runtime canary the harness cannot
model accurately.
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


def _settings() -> Settings:
    return Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )


def test_supports_runtime_canary_accepts_only_sizing_multiplier(
    tmp_path: Path,
) -> None:
    controller = ExperimentController(
        settings=_settings(),
        store=None,  # type: ignore[arg-type]
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    allowed = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )
    assert controller.supports_runtime_canary(allowed) is True


def test_supports_runtime_canary_rejects_other_parameters(
    tmp_path: Path,
) -> None:
    controller = ExperimentController(
        settings=_settings(),
        store=None,  # type: ignore[arg-type]
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    not_allowed = [
        ParameterChange(
            section="supermodel",
            field="support_threshold",
            baseline=0.55,
            candidate=0.50,
        ),
        ParameterChange(
            section="supermodel",
            field="counter_veto_weight",
            baseline=0.75,
            candidate=0.50,
        ),
        ParameterChange(
            section="strategy_tracker",
            field="full_allocation_rate",
            baseline=0.10,
            candidate=0.05,
        ),
        ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.0,
        ),
    ]

    for change in not_allowed:
        assert controller.supports_runtime_canary(change) is False, change


def test_record_canary_snapshot_writes_runtime_metrics(
    tmp_path: Path,
) -> None:
    """The snapshot populates candidate_metrics from the runtime paired
    candidate ledger and shadow_metrics from the paired baseline ledger.
    Offline baseline_metrics remains unchanged.
    """
    from trading_bot.learning.experiments.store import ExperimentStore

    store = ExperimentStore(root=tmp_path / "experiments")
    controller = ExperimentController(
        settings=_settings(),
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    offline_baseline = MetricSet(
        trades=24,
        profit_factor=1.20,
        net_pnl=320.0,
        max_drawdown_pct=2.0,
    )
    state = ExperimentState(
        experiment_id="rt-snapshot",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        baseline_metrics=offline_baseline,
        candidate_metrics=None,
        shadow_metrics=None,
    )

    class _Harness:
        def __init__(self) -> None:
            self.calls = 0

        def candidate_metrics(self):
            from trading_bot.learning.experiments.models import MetricSet

            self.calls += 1
            return MetricSet(
                trades=5,
                profit_factor=1.10,
                net_pnl=80.0,
                max_drawdown_pct=1.0,
            )

        def baseline_metrics(self):
            from trading_bot.learning.experiments.models import MetricSet

            return MetricSet(
                trades=5,
                profit_factor=1.05,
                net_pnl=60.0,
                max_drawdown_pct=1.2,
            )

        def closed_trade_counts_match(self):
            return True

    harness = _Harness()

    new_state = controller.record_canary_snapshot(state, harness)  # type: ignore[arg-type]

    assert harness.calls == 1
    assert new_state.candidate_metrics is not None
    assert new_state.shadow_metrics is not None
    assert new_state.candidate_metrics.trades == 5
    assert new_state.shadow_metrics.trades == 5
    assert new_state.canary_closed_trades == 5
    # Offline baseline_metrics must remain untouched.
    assert new_state.baseline_metrics == offline_baseline  # type: ignore[comparison-overlap]  


def test_evaluate_marks_unsupported_change_inconclusive(
    tmp_path: Path,
) -> None:
    """evaluate() refuses to advance an unsupported change to CANARY.
    Unsupported changes land in INCONCLUSIVE with baseline restored.
    """
    from datetime import datetime, timezone

    from trading_bot.learning.experiments.controller import ExperimentController
    from trading_bot.learning.experiments.store import ExperimentStore
    from trading_bot.portfolio.ledger import PortfolioLedger

    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "supermodel:\n  support_threshold: 0.55\n", encoding="utf-8"
    )

    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")

    store = ExperimentStore(root=tmp_path / "experiments")
    # Make sure the live portfolio is flat so we exercise the support check
    # rather than the non-flat guard.
    ledger = PortfolioLedger(Path(settings.app.state_db_path))
    state_obj = ledger.ensure_portfolio_state()
    ledger.save_portfolio_state(state_obj.model_copy(update={"positions": {}}))

    state = ExperimentState(
        experiment_id="rt-unsupported",
        status="PROPOSED",
        change=ParameterChange(
            section="supermodel",
            field="support_threshold",
            baseline=0.55,
            candidate=0.50,
        ),
        started_at=datetime.now(timezone.utc),
    )
    store.save_current(state)

    # Skip the offline stage by jumping directly to CANARY.
    state.status = "CANARY"
    state.runtime_canary_armed = True
    state.baseline_metrics = {
        "trades": 24,
        "profit_factor": 1.20,
        "net_pnl": 320.0,
        "max_drawdown_pct": 2.0,
    }
    store.save_current(state)

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )

    decided = controller.evaluate()

    assert decided is not None
    assert decided.status == "INCONCLUSIVE"
    assert decided.last_error is not None
    assert "unsupported_runtime_canary" in decided.last_error


def test_evaluate_marks_non_flat_portfolio_inconclusive(
    tmp_path: Path,
) -> None:
    """If the live portfolio is not flat at canary start, the canary is
    INCONCLUSIVE rather than attempting to launch with prior positions.
    """
    from datetime import datetime, timezone

    from trading_bot.learning.experiments.controller import ExperimentController
    from trading_bot.learning.experiments.store import ExperimentStore
    from trading_bot.models.portfolio import Position, PortfolioState
    from trading_bot.portfolio.ledger import PortfolioLedger

    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n",
        encoding="utf-8",
    )

    settings = _settings()
    settings.app.state_db_path = str(tmp_path / "state.db")

    store = ExperimentStore(root=tmp_path / "experiments")

    state = ExperimentState(
        experiment_id="rt-nonflat",
        status="CANARY",
        runtime_canary_armed=True,
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        baseline_metrics={
            "trades": 24,
            "profit_factor": 1.20,
            "net_pnl": 320.0,
            "max_drawdown_pct": 2.0,
        },
    )
    store.save_current(state)

    # Inject a non-flat portfolio directly via PortfolioState construction.
    ledger = PortfolioLedger(Path(settings.app.state_db_path))
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

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )

    decided = controller.evaluate()
    assert decided is not None
    assert decided.status == "INCONCLUSIVE"
    assert decided.last_error is not None
    assert "non_flat_portfolio_on_canary_start" in decided.last_error
