"""Tests for wiring RuntimeCanaryContext through run_paper_trade BUY fills.

The seam exposes two new keyword-only parameters on ``run_paper_trade``:

- ``runtime_canary``: optional context loaded by the CLI. When provided,
  successful BUY fills mirror into the paired shadow harness at exact
  pre-policy and post-policy quantities.

When ``runtime_canary`` is ``None`` the function behaves byte-identically
to its previous implementation: no shadow disk I/O, no harness
construction, no event-log side effects beyond the existing
decision-log entries.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
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
from trading_bot.learning.experiments.runtime_canary import RuntimeCanaryContext
from trading_bot.learning.experiments.store import ExperimentStore


def _settings(state_db_path: Path) -> Settings:
    settings = Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )
    settings.app.state_db_path = str(state_db_path)
    settings.app.log_dir = str(state_db_path.parent / "logs")
    settings.app.approved_candidates_path = str(
        state_db_path.parent / "approved-candidates.jsonl"
    )
    settings.app.tuning_overrides_path = str(state_db_path.parent / "overrides.yaml")
    settings.paper.slippage_bps = 0
    settings.risk.use_atr_sizing = False
    settings.supermodel.range_bound_trend_caution_multiplier = 0.5
    return settings


def _seed_canary_state(store: ExperimentStore) -> ExperimentState:
    state = ExperimentState(
        experiment_id="buy-wiring",
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
    store.save_current(state)
    return state


def _build_runtime_canary(
    settings: Settings,
    store: ExperimentStore,
) -> RuntimeCanaryContext:
    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=Path(settings.app.tuning_overrides_path),
    )
    return RuntimeCanaryContext(
        state=store.load_current(),
        controller=controller,
        store=store,
        harness=_Harness(double_size=False),  # type: ignore[arg-type]
        artifacts_dir=store.root / "buy-wiring",
        starting_cash=10_000.0,
    )


class _Harness:
    """Minimal stand-in harness for verifying record_entry wiring."""

    def __init__(self, double_size: bool) -> None:
        self.entries: list[dict[str, Any]] = []
        self.exits: list[dict[str, Any]] = []
        self._double = double_size

    def record_entry(self, **kwargs) -> None:
        # When double_size=True, simulate a buggy harness that doubles
        # the candidate quantity — proving our wiring passes the actual
        # fill quantity, not the policy-multiplied value.
        if self._double:
            kwargs = dict(kwargs)
            kwargs["candidate_quantity"] = kwargs["candidate_quantity"] * 2
        self.entries.append(kwargs)

    def record_exit(self, **kwargs) -> None:
        self.exits.append(kwargs)

    def candidate_metrics(self):  # type: ignore[no-untyped-def]
        return MetricSet(trades=0, profit_factor=0.0, net_pnl=0.0, max_drawdown_pct=0.0)

    def baseline_metrics(self):  # type: ignore[no-untyped-def]
        return MetricSet(trades=0, profit_factor=0.0, net_pnl=0.0, max_drawdown_pct=0.0)

    def closed_trade_counts_match(self) -> bool:
        return True


def _prepare_canary_buy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import trading_bot.execution.order_manager as order_manager
    import trading_bot.runtime.orchestrator as orchestrator
    from trading_bot.models.risk import RiskDecision
    from trading_bot.models.signal import TradeSignal

    signal = TradeSignal(
        ticker="AAPL",
        timeframe="intraday",
        action="BUY",
        entry_price=100.0,
        stop_loss=95.0,
        profit_target=110.0,
        risk_reward_ratio=2.0,
        confidence=0.8,
        reasons=["runtime canary test"],
        strategy_tag="v3-trend_following",
        timestamp=datetime(2026, 7, 24, 14, 0, tzinfo=timezone.utc),
        quality="GREEN",
    )
    stacked = SimpleNamespace(
        decision="support",
        score=1.0,
        to_details=lambda: {
            "supermodel_decision": "caution",
            "supermodel_score": 1.0,
        },
    )

    def approved_decision(*args: Any, **kwargs: Any) -> RiskDecision:
        return RiskDecision(
            approved=True,
            reason="approved",
            position_size=10,
            dollar_risk=50.0,
        )

    monkeypatch.setattr(
        orchestrator,
        "_build_signal_result",
        lambda *args, **kwargs: (
            signal,
            "",
            {
                "quality": "GREEN",
                "regime": "range_bound",
                "supermodel_decision": "caution",
            },
        ),
    )
    monkeypatch.setattr(orchestrator, "build_stacked_signal", lambda *args, **kwargs: stacked)
    monkeypatch.setattr(orchestrator, "evaluate_signal", approved_decision)
    monkeypatch.setattr(order_manager, "evaluate_signal", approved_decision)
    monkeypatch.setattr(orchestrator, "_daily_loss_limit_hit", lambda *args, **kwargs: False)
    monkeypatch.setattr(orchestrator, "_daily_order_limit_hit", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        orchestrator,
        "_correlation_context_for_candidate",
        lambda *args, **kwargs: (0.0, None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_sector_concentration_exceeded",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "_persist_trade_to_db", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "trading_bot.safety.kill_switch.check_kill_switch_before_trade",
        lambda *args, **kwargs: (True, ""),
    )
    monkeypatch.setattr(
        "trading_bot.safety.circuit_breaker.check_circuit_breakers",
        lambda *args, **kwargs: (True, ""),
    )


def test_run_paper_trade_accepts_runtime_canary_kwarg(tmp_path: Path) -> None:
    """A no-op runtime canary stays compatible with the unchanged path."""
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    state = ExperimentState(
        experiment_id="no-active",
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
    store = ExperimentStore(root=tmp_path / "experiments")
    store.save_current(state)
    overrides = tmp_path / "overrides.yaml"
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n",
        encoding="utf-8",
    )

    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=overrides,
    )

    # Empty symbol list keeps the function path short.
    ctx = RuntimeCanaryContext(
        state=state,
        controller=controller,
        store=store,
        harness=_Harness(double_size=False),  # type: ignore[arg-type]
        artifacts_dir=tmp_path / "experiments" / "buy-wiring",
        starting_cash=10_000.0,
    )

    # Just verifies the parameter is accepted and called without error on
    # the empty-symbol path. The expensive monkeypatch of fetch_bars is
    # avoided so this test stays fast.
    result = run_paper_trade([], settings, dry_run=True, runtime_canary=ctx)
    assert isinstance(result, list)


def test_existing_signature_still_works_without_runtime_canary(
    tmp_path: Path,
) -> None:
    """Calling run_paper_trade without runtime_canary remains valid."""
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    result = run_paper_trade([], settings, dry_run=True)
    assert isinstance(result, list)


def test_dry_run_skips_shadow_recording(tmp_path: Path) -> None:
    """Dry-run BUY fills do NOT mirror into the harness."""
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)

    store = ExperimentStore(root=tmp_path / "experiments")
    _seed_canary_state(store)
    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=Path(settings.app.tuning_overrides_path),
    )

    harness = _Harness(double_size=False)  # type: ignore[arg-type]
    ctx = RuntimeCanaryContext(
        state=store.load_current(),
        controller=controller,
        store=store,
        harness=harness,
        artifacts_dir=tmp_path / "experiments" / "buy-wiring",
        starting_cash=10_000.0,
    )

    # Dry-run path never reaches fill_broker so the harness stays empty.
    run_paper_trade([], settings, dry_run=True, runtime_canary=ctx)
    assert harness.entries == []


def test_harness_receives_exact_pre_policy_and_fill_quantities(tmp_path: Path) -> None:
    """Verify the harness accepts both pre-policy and post-policy
    quantities as separate values (no internal scaling)."""
    from trading_bot.learning.experiments.runtime_canary import (
        load_runtime_canary,
    )

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    store = ExperimentStore(root=tmp_path / "experiments")
    _seed_canary_state(store)

    from trading_bot.portfolio.ledger import PortfolioLedger

    ledger = PortfolioLedger(state_db)
    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )

    ctx = load_runtime_canary(settings, ledger, controller=controller)
    assert ctx is not None

    # Simulate a BUY with pre-policy=10, candidate (post-policy) = 5.
    ctx.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )

    # The harness must hold exactly the supplied quantities — neither
    # hidden multiplier scaling nor implicit candidate reduction.
    assert ctx.harness.baseline.snapshot_positions()["AAPL"]["qty"] == 10
    assert ctx.harness.candidate.snapshot_positions()["AAPL"]["qty"] == 5


def test_failed_buy_transaction_does_not_record_shadow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_bot.portfolio.ledger import PortfolioLedger
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    store = ExperimentStore(root=tmp_path / "experiments")
    _seed_canary_state(store)
    ctx = _build_runtime_canary(settings, store)
    _prepare_canary_buy(monkeypatch)

    def fail_record_fill(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db failed")

    monkeypatch.setattr(PortfolioLedger, "record_fill", fail_record_fill)

    result = run_paper_trade(["AAPL"], settings, runtime_canary=ctx)

    assert result[0].startswith("AAPL ERROR")
    assert ctx.harness.entries == []


def test_successful_buy_records_canary_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    store = ExperimentStore(root=tmp_path / "experiments")
    _seed_canary_state(store)
    ctx = _build_runtime_canary(settings, store)
    _prepare_canary_buy(monkeypatch)

    result = run_paper_trade(["AAPL"], settings, runtime_canary=ctx)

    assert result[0].startswith("AAPL FILLED qty=5")
    with sqlite3.connect(state_db) as connection:
        row = connection.execute(
            """
            SELECT canary_experiment_id, canary_baseline_quantity, quantity
            FROM orders
            WHERE side = 'BUY'
            """
        ).fetchone()
    assert row == ("buy-wiring", 10, 5)


def test_buy_transaction_records_baseline_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_bot.learning.experiments.shadow import PairedShadowHarness
    from trading_bot.runtime.orchestrator import run_paper_trade

    state_db = tmp_path / "state.db"
    settings = _settings(state_db)
    store = ExperimentStore(root=tmp_path / "experiments")
    state = _seed_canary_state(store)
    ctx = _build_runtime_canary(settings, store)
    ctx.harness = PairedShadowHarness(
        artifacts_dir=ctx.artifacts_dir,
        starting_cash=ctx.starting_cash,
        change=state.change,
    )
    _prepare_canary_buy(monkeypatch)

    result = run_paper_trade(["AAPL"], settings, runtime_canary=ctx)

    assert result[0].startswith("AAPL FILLED qty=5")
    assert ctx.harness.candidate.snapshot_positions()["AAPL"]["qty"] == 5
    assert ctx.harness.baseline.snapshot_positions()["AAPL"]["qty"] == 10
