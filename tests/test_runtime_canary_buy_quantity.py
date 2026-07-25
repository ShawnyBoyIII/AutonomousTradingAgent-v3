"""Regression test for the runtime canary BUY quantity contract.

When a 0.5 multiplier is applied, the live candidate fill uses the
already-halved size. The baseline (shadow) ledger must record the
pre-policy size so the comparison is meaningful.

Reproduction of the original bug:
    - Risk sizing produces 20 shares.
    - 0.5 multiplier produces decision.position_size = 10.
    - The fill uses 10 shares.
    - Before fix: baseline_quantity = 10, candidate_quantity = 5.
    - After fix: baseline_quantity = 20, candidate_quantity = 10.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from trading_bot.learning.experiments.models import (
    ExperimentState,
    MetricSet,
    ParameterChange,
)
from trading_bot.learning.experiments.runtime_canary import RuntimeCanaryContext
from trading_bot.learning.experiments.store import ExperimentStore


class _HarnessSpy:
    """Captures record_entry/record_exit calls verbatim."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.exits: list[dict[str, Any]] = []

    def record_entry(self, **kwargs) -> None:
        self.entries.append(kwargs)

    def record_exit(self, **kwargs) -> None:
        self.exits.append(kwargs)

    def candidate_metrics(self):
        return MetricSet(trades=0, profit_factor=0.0, net_pnl=0.0, max_drawdown_pct=0.0)

    def baseline_metrics(self):
        return MetricSet(trades=0, profit_factor=0.0, net_pnl=0.0, max_drawdown_pct=0.0)

    def closed_trade_counts_match(self) -> bool:
        return True


def _ctx(tmp_path: Path, harness: _HarnessSpy) -> RuntimeCanaryContext:
    from trading_bot.config.settings import (
        PaperSettings,
        Settings,
        StrategySettings,
        StrategyTrackerSettings,
        SupermodelSettings,
    )
    from trading_bot.learning.experiments.controller import ExperimentController

    settings = Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )
    settings.app.state_db_path = str(tmp_path / "state.db")
    settings.app.tuning_overrides_path = str(tmp_path / "overrides.yaml")

    store = ExperimentStore(root=tmp_path / "experiments")
    state = ExperimentState(
        experiment_id="buy-quantity-contract",
        status="CANARY",
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
        started_at=datetime.now(timezone.utc),
        runtime_canary_armed=True,
    )
    store.save_current(state)
    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=None,
        overrides_path=tmp_path / "overrides.yaml",
    )
    return RuntimeCanaryContext(
        state=state,
        controller=controller,
        store=store,
        harness=harness,
        artifacts_dir=tmp_path / "experiments" / "buy-quantity-contract",
        starting_cash=100_000.0,
    )


def test_record_entry_carries_baseline_and_candidate_quantities(tmp_path: Path) -> None:
    """record_entry must persist exactly the (baseline, candidate) pair the
    caller passes — not reapply the multiplier or mutate either side.
    """
    harness = _HarnessSpy()
    ctx = _ctx(tmp_path, harness)
    ctx.record_entry(
        ticker="AAPL",
        baseline_quantity=20,
        candidate_quantity=10,
        fill_price=150.0,
        fees=1.0,
        session_date="2026-07-22",
    )
    assert harness.entries == [
        {
            "operation_id": "",
            "ticker": "AAPL",
            "baseline_quantity": 20,
            "candidate_quantity": 10,
            "fill_price": 150.0,
            "fees": 1.0,
        }
    ]


def test_record_entry_preserves_unrelated_quantity_differences(tmp_path: Path) -> None:
    """Sanity check: the helper does not collapse a 0.5 multiplier
    candidate to half of an already-halved size. The caller passes the
    baseline and candidate sizes; the helper must not reinterpret them.
    """
    harness = _HarnessSpy()
    ctx = _ctx(tmp_path, harness)
    ctx.record_entry(
        ticker="AAPL",
        baseline_quantity=100,
        candidate_quantity=100,  # No policy applied, both equal
        fill_price=100.0,
        fees=1.0,
    )
    assert harness.entries[0]["baseline_quantity"] == 100
    assert harness.entries[0]["candidate_quantity"] == 100
