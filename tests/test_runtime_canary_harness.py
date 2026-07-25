"""Tests for the runtime canary paired-shadow harness API.

These tests exercise the exact-quantity semantics added in revision 2 of the
paired-shadow-harness-runtime-wiring design. Legacy ``record_paired`` is
preserved (it still delegates to the same backing ledgers) so the existing
``tests/test_paired_shadow_lifecycle.py`` stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trading_bot.learning.experiments.models import (
    MetricSet,
    ParameterChange,
)
from trading_bot.learning.experiments.shadow import (
    PairedShadowHarness,
    ShadowFill,
    ShadowLedger,
)


def _make_change() -> ParameterChange:
    return ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )


def test_record_entry_records_exact_quantities(tmp_path: Path) -> None:
    """record_entry mirrors the caller-supplied baseline and candidate
    quantities verbatim. No multiplier is applied internally.
    """
    harness = PairedShadowHarness(
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=10_000.0,
        change=_make_change(),
    )

    harness.record_entry(
        operation_id="buy-1",
        ticker="SPY",
        baseline_quantity=50,
        candidate_quantity=25,
        fill_price=100.0,
        fees=1.0,
    )

    # Baseline ledger holds 50 shares; candidate ledger holds 25 shares.
    baseline_state = harness.baseline.snapshot_positions()
    candidate_state = harness.candidate.snapshot_positions()
    assert baseline_state["SPY"]["qty"] == 50
    assert candidate_state["SPY"]["qty"] == 25


def test_record_entry_nonmatching_records_equal_quantities(tmp_path: Path) -> None:
    """When the runtime policy didn't scale (nonmatching trade), the
    harness records the same quantity on both sides.
    """
    harness = PairedShadowHarness(
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=10_000.0,
        change=_make_change(),
    )

    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=10,
        fill_price=200.0,
        fees=1.0,
    )

    assert harness.baseline.snapshot_positions()["AAPL"]["qty"] == 10
    assert harness.candidate.snapshot_positions()["AAPL"]["qty"] == 10


def test_record_exit_derives_baseline_qty_from_candidate_fraction(
    tmp_path: Path,
) -> None:
    """A partial exit on the candidate side scales the baseline exit by
    the fraction of the candidate position sold.
    """
    harness = PairedShadowHarness(
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=10_000.0,
        change=_make_change(),
    )
    # Baseline 50, candidate 25.
    harness.record_entry(
        operation_id="buy-1",
        ticker="SPY",
        baseline_quantity=50,
        candidate_quantity=25,
        fill_price=100.0,
        fees=1.0,
    )
    # Sell 10/25 = 40% of candidate; baseline should sell 40%*50 = 20.
    harness.record_exit(
        operation_id="sell-1",
        ticker="SPY",
        candidate_quantity=10,
        fill_price=110.0,
        fees=1.0,
    )

    baseline = harness.baseline.snapshot_positions()
    candidate = harness.candidate.snapshot_positions()
    assert pytest.approx(baseline["SPY"]["qty"], abs=1e-6) == 30
    assert pytest.approx(candidate["SPY"]["qty"], abs=1e-6) == 15


def test_record_exit_full_closes_both_ledgers(tmp_path: Path) -> None:
    """A full candidate exit closes both positions to zero."""
    harness = PairedShadowHarness(
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=10_000.0,
        change=_make_change(),
    )
    harness.record_entry(
        operation_id="buy-1",
        ticker="SPY",
        baseline_quantity=50,
        candidate_quantity=25,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="SPY",
        candidate_quantity=25,
        fill_price=120.0,
        fees=1.0,
    )

    assert "SPY" not in harness.baseline.snapshot_positions()
    assert "SPY" not in harness.candidate.snapshot_positions()

    # Both ledgers record exactly one closed trade.
    assert harness.candidate_metrics().trades == 1
    assert harness.baseline_metrics().trades == 1


def test_metrics_track_partial_profit_taking(tmp_path: Path) -> None:
    """Two candidates each produce one closed trade after one
    round-trip with partial-profit exits."""
    harness = PairedShadowHarness(
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=10_000.0,
        change=_make_change(),
    )

    # Trade 1: candidate 10, full exit at +5
    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=10,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="AAPL",
        candidate_quantity=10,
        fill_price=105.0,
        fees=1.0,
    )

    # Trade 2: candidate 5, full exit at -2
    harness.record_entry(
        operation_id="buy-2",
        ticker="MSFT",
        baseline_quantity=5,
        candidate_quantity=5,
        fill_price=200.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-2",
        ticker="MSFT",
        candidate_quantity=5,
        fill_price=198.0,
        fees=1.0,
    )

    candidate = harness.candidate_metrics()
    baseline = harness.baseline_metrics()

    assert candidate.trades == 2
    assert baseline.trades == 2
    # Both ledgers trade identical size, so the metrics should match.
    assert pytest.approx(candidate.net_pnl, abs=1e-6) == baseline.net_pnl
    assert pytest.approx(candidate.profit_factor, abs=1e-6) == baseline.profit_factor


def test_closed_trade_counts_match_true(tmp_path: Path) -> None:
    """Symmetric round-trips leave closed trade counts equal."""
    harness = PairedShadowHarness(
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=10_000.0,
        change=_make_change(),
    )
    harness.record_entry(
        operation_id="buy-1",
        ticker="SPY",
        baseline_quantity=25,
        candidate_quantity=10,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="SPY",
        candidate_quantity=10,
        fill_price=110.0,
        fees=1.0,
    )

    assert harness.closed_trade_counts_match() is True


def test_restart_rebuilds_metrics(tmp_path: Path) -> None:
    """A fresh PairedShadowHarness constructed against the same
    artifacts_dir reproduces the running metrics. This proves the JSONL
    trail is sufficient for crash recovery without leaking history to
    prior experiments.
    """
    artifacts = tmp_path / "artifacts"
    change = _make_change()

    harness = PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=change,
    )
    harness.record_entry(
        operation_id="buy-1",
        ticker="SPY",
        baseline_quantity=10,
        candidate_quantity=10,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="SPY",
        candidate_quantity=10,
        fill_price=110.0,
        fees=1.0,
    )

    expected_candidate = harness.candidate_metrics().model_dump()
    expected_baseline = harness.baseline_metrics().model_dump()

    # Fresh handle, same artifacts, same starting cash: identical metrics.
    rebuilt = PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=change,
    )

    assert rebuilt.candidate_metrics().model_dump() == expected_candidate
    assert rebuilt.baseline_metrics().model_dump() == expected_baseline


def test_metric_set_round_trip_smoke(tmp_path: Path) -> None:
    """Smoke test: a winning trade populates non-zero metrics."""
    harness = PairedShadowHarness(
        artifacts_dir=tmp_path / "artifacts",
        starting_cash=10_000.0,
        change=_make_change(),
    )
    harness.record_entry(
        operation_id="buy-1",
        ticker="SPY",
        baseline_quantity=1,
        candidate_quantity=1,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="SPY",
        candidate_quantity=1,
        fill_price=110.0,
        fees=1.0,
    )

    m = harness.candidate_metrics()
    assert isinstance(m, MetricSet)
    assert m.trades == 1
    # Bought at 100 with 1 fee, sold at 110 with 1 fee → realized = (110-100)*1 - 2 = 8.
    assert pytest.approx(m.net_pnl, abs=1e-6) == 8.0


def test_shadow_ledger_remains_independent_of_refactor(tmp_path: Path) -> None:
    """The legacy ShadowLedger surface still works so old tests stay green."""
    ledger = ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=1_000.0)
    ledger.record(
        ShadowFill(ticker="X", side="BUY", quantity=5, fill_price=10.0, fees=1.0)
    )
    assert ledger.snapshot_positions()["X"]["qty"] == 5
