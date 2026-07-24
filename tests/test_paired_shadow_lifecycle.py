"""TDD: PairedShadowHarness independent-size routing, equity curves, defensive ignores.

Exercises the production API of PairedShadowHarness (record_entry,
record_exit) rather than the removed record_paired wrapper. Each
test pins one behavior the runtime canary depends on.
"""
from __future__ import annotations

import json
from pathlib import Path

from trading_bot.learning.experiments.models import ParameterChange
from trading_bot.learning.experiments.shadow import PairedShadowHarness


def _harness(artifacts: Path, *, baseline: float = 1.0, candidate: float = 0.5) -> PairedShadowHarness:
    return PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=baseline,
            candidate=candidate,
        ),
        baseline_multiplier=baseline,
        candidate_multiplier=candidate,
    )


def test_paired_shadow_routes_each_fill_at_independent_size(tmp_path: Path) -> None:
    """record_entry with explicit baseline/candidate quantities must
    produce different closed-trade P&L on the two ledgers so the canary
    gate compares apples-to-apples economics, not the same fill twice.
    """
    artifacts = tmp_path / "artifacts"
    harness = _harness(artifacts)

    harness.record_entry(
        ticker="SPY",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        ticker="SPY",
        candidate_quantity=5,
        fill_price=110.0,
        fees=1.0,
    )

    candidate_metrics = harness.candidate.metrics()
    baseline_metrics = harness.baseline.metrics()

    # Both ledgers record one closed trade.
    assert baseline_metrics.trades == 1
    assert candidate_metrics.trades == 1
    # Baseline: BUY 10@100 + SELL 10@110 = (1100 - 1000) - 2 = 98
    assert abs(baseline_metrics.net_pnl - 98.0) < 0.01
    # Candidate: BUY 5@100 + SELL 5@110 = (550 - 500) - 2 = 48
    assert abs(candidate_metrics.net_pnl - 48.0) < 0.01
    assert (artifacts / "shadow-fills.jsonl").exists()
    assert (artifacts / "candidate-shadow-fills.jsonl").exists()


def test_paired_shadow_baseline_records_equity_curves(tmp_path: Path) -> None:
    """Both ledgers must persist equity curves so canary metrics can be
    reconstructed after a process restart.
    """
    artifacts = tmp_path / "artifacts"
    harness = _harness(artifacts)

    harness.record_entry(
        ticker="SPY",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        ticker="SPY",
        candidate_quantity=5,
        fill_price=110.0,
        fees=1.0,
    )

    candidate_equity = [
        json.loads(line)["equity"]
        for line in (artifacts / "candidate-shadow-equity.jsonl").read_text().splitlines()
    ]
    baseline_equity = [
        json.loads(line)["equity"]
        for line in (artifacts / "shadow-equity.jsonl").read_text().splitlines()
    ]
    assert len(candidate_equity) == 2
    assert len(baseline_equity) == 2
    # Baseline ends at 10000 + 98 = 10098
    baseline_last = baseline_equity[-1]
    assert abs(baseline_last - 10_098.0) < 1.0
    # Candidate ends at 10000 + 48 = 10048
    candidate_last = candidate_equity[-1]
    assert abs(candidate_last - 10_048.0) < 1.0


def test_paired_shadow_ignores_zero_and_negative_quantities(tmp_path: Path) -> None:
    """The harness must skip zero/negative quantities defensively so
    upstream partial-fill anomalies do not corrupt the ledgers.
    """
    artifacts = tmp_path / "artifacts"
    harness = PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=ParameterChange(
            section="supermodel",
            field="range_bound_trend_caution_multiplier",
            baseline=1.0,
            candidate=0.5,
        ),
    )

    harness.record_entry(ticker="SPY", baseline_quantity=0, candidate_quantity=0,
                         fill_price=100.0, fees=0.0)
    harness.record_entry(ticker="SPY", baseline_quantity=-1, candidate_quantity=-1,
                         fill_price=100.0, fees=0.0)
    harness.record_exit(ticker="SPY", candidate_quantity=0,
                        fill_price=110.0, fees=1.0)
    harness.record_exit(ticker="SPY", candidate_quantity=-5,
                        fill_price=110.0, fees=1.0)

    assert harness.baseline.metrics().trades == 0
    assert harness.candidate.metrics().trades == 0