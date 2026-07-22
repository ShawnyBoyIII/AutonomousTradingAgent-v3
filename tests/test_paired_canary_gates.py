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
from trading_bot.learning.experiments.shadow import ShadowLedger


def _settings() -> Settings:
    return Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )


def test_early_rollback_when_pf_below_floor(tmp_path: Path) -> None:
    state = ExperimentState(
        experiment_id="test_early_pf",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        canary_closed_trades=15,
        market_sessions=["2026-07-18", "2026-07-19"],
        candidate_metrics=MetricSet(
            trades=15,
            profit_factor=0.4,  # below EARLY_PF_FLOOR=0.50
            net_pnl=-200.0,
            max_drawdown_pct=3.0,
        ),
        shadow_metrics=MetricSet(
            trades=15,
            profit_factor=0.7,
            net_pnl=-50.0,
            max_drawdown_pct=2.0,
        ),
    )
    controller = ExperimentController(
        settings=_settings(),
        store=None,  # type: ignore[arg-type]
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    decision = controller._decide(state)
    assert decision == "ROLLED_BACK"


def test_keep_when_candidate_beats_shadow(tmp_path: Path) -> None:
    state = ExperimentState(
        experiment_id="test_keep",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        canary_closed_trades=25,
        market_sessions=["2026-07-18", "2026-07-19", "2026-07-20"],
        candidate_metrics=MetricSet(
            trades=25,
            profit_factor=1.20,
            net_pnl=150.0,
            max_drawdown_pct=3.0,
        ),
        shadow_metrics=MetricSet(
            trades=25,
            profit_factor=1.00,
            net_pnl=80.0,
            max_drawdown_pct=3.5,
        ),
    )
    controller = ExperimentController(
        settings=_settings(),
        store=None,  # type: ignore[arg-type]
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    decision = controller._decide(state)
    assert decision == "KEPT"


def test_inconclusive_when_sessions_exceed_timeout_without_min_trades(tmp_path: Path) -> None:
    state = ExperimentState(
        experiment_id="test_inconclusive",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        canary_closed_trades=5,  # below MIN_CANARY_TRADES=20
        market_sessions=[f"2026-07-{day:02d}" for day in range(8, 20)],  # above TIMEOUT_SESSIONS=10
        candidate_metrics=MetricSet(
            trades=5,
            profit_factor=0.8,
            net_pnl=-20.0,
            max_drawdown_pct=1.0,
        ),
        shadow_metrics=MetricSet(
            trades=5,
            profit_factor=0.7,
            net_pnl=-30.0,
            max_drawdown_pct=1.5,
        ),
    )
    controller = ExperimentController(
        settings=_settings(),
        store=None,  # type: ignore[arg-type]
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    decision = controller._decide(state)
    assert decision == "INCONCLUSIVE"


def test_shadow_ledger_records_equity_curve_separately(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    ledger = ShadowLedger(artifacts, starting_cash=10_000.0)
    ledger.record(
        ShadowLedger.__class__.__mro__[0].__call__  # type: ignore[attr-defined]
    ) if False else None  # silence linter
    from trading_bot.learning.experiments.shadow import ShadowFill
    ledger.record(ShadowFill(ticker="SPY", side="BUY", quantity=10, fill_price=100.0, fees=1.0))

    fills = (artifacts / "shadow-fills.jsonl").read_text().splitlines()
    equity = (artifacts / "shadow-equity.jsonl").read_text().splitlines()
    assert len(fills) == 1
    assert len(equity) == 1
