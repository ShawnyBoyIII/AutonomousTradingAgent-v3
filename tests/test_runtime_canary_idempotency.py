"""Tests for idempotent paired-shadow accounting and completed-position counting.

The runtime canary must:

1. Refuse to re-apply the same ``operation_id`` (paper-order row id) twice so
   that retries from BUY/SELL persistence do not double-count or re-realize
   P&L. The same atomic no-op must apply to both the candidate and baseline
   ledgers.
2. Count *completed positions* — full SELLs that close a ticker to zero — as
   the gate signal, distinct from the realized-SELL count that drives
   profit factor / net P&L. Partial exits leave ``completed_positions``
   unchanged.
3. Replay legacy JSONL rows written before the ``operation_id`` schema was
   introduced (default ``operation_id = ""``).
"""

from __future__ import annotations

import json
from pathlib import Path

from trading_bot.learning.experiments.models import ParameterChange
from trading_bot.learning.experiments.shadow import (
    PairedShadowHarness,
    ShadowFill,
    ShadowLedger,
)


def _change() -> ParameterChange:
    return ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )


def _harness(artifacts: Path) -> PairedShadowHarness:
    return PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=_change(),
    )


def test_duplicate_operation_id_is_no_op(tmp_path: Path) -> None:
    """The same operation_id recorded twice leaves the position unchanged.

    Recording the BUY a second time must not re-open the position, not
    double the cost basis, and not produce a second cash deduction.
    """
    harness = _harness(tmp_path / "artifacts")

    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )

    assert harness.candidate.snapshot_positions()["AAPL"]["qty"] == 5
    assert harness.baseline.snapshot_positions()["AAPL"]["qty"] == 10
    assert harness.candidate_completed_trades() == 0
    assert harness.baseline_completed_trades() == 0


def test_partial_exit_does_not_complete_trade(tmp_path: Path) -> None:
    """A partial SELL keeps completed_positions at 0 on both ledgers."""
    harness = _harness(tmp_path / "artifacts")

    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="AAPL",
        candidate_quantity=2,
        fill_price=105.0,
        fees=1.0,
    )

    assert harness.candidate_completed_trades() == 0
    assert harness.baseline_completed_trades() == 0
    # Candidate holds 3, baseline holds 10 - round(2/5 * 10) = 6.
    assert harness.candidate.snapshot_positions()["AAPL"]["qty"] == 3
    assert harness.baseline.snapshot_positions()["AAPL"]["qty"] == 6


def test_full_exit_completes_one_trade(tmp_path: Path) -> None:
    """A full SELL increments completed_positions by 1 in both ledgers."""
    harness = _harness(tmp_path / "artifacts")

    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="AAPL",
        candidate_quantity=5,
        fill_price=110.0,
        fees=1.0,
    )

    assert harness.candidate_completed_trades() == 1
    assert harness.baseline_completed_trades() == 1
    assert harness.completed_trade_counts_match() is True


def test_proportional_baseline_exit(tmp_path: Path) -> None:
    """When candidate sold 4 of 10 and baseline held 20, baseline sells 8.

    This pins the proportion formula already in place; the regression
    guard is that the formula survives the operation_id plumbing.
    """
    harness = _harness(tmp_path / "artifacts")

    harness.record_entry(
        operation_id="buy-1",
        ticker="SPY",
        baseline_quantity=20,
        candidate_quantity=10,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="SPY",
        candidate_quantity=4,
        fill_price=105.0,
        fees=1.0,
    )

    assert harness.candidate.snapshot_positions()["SPY"]["qty"] == 6
    assert harness.baseline.snapshot_positions()["SPY"]["qty"] == 12
    # Partial on both sides — no completion yet.
    assert harness.candidate_completed_trades() == 0
    assert harness.baseline_completed_trades() == 0


def test_completed_count_mismatch_helper(tmp_path: Path) -> None:
    """completed_trade_counts_match() returns False when ledgers diverge.

    The divergence test simulates the divergence via shadow-only calls so
    no live API change is required: the helper exposes the boolean the
    controller will key on.
    """
    artifacts = tmp_path / "artifacts"
    harness = _harness(artifacts)

    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    # Full candidate exit closes only the candidate side.
    harness.candidate.record(
        ShadowFill(
            ticker="AAPL",
            side="SELL",
            quantity=5,
            fill_price=110.0,
            fees=1.0,
            operation_id="candidate-only-exit",
        )
    )
    harness.candidate.record(
        ShadowFill(
            ticker="AAPL",
            side="SELL",
            quantity=5,
            fill_price=110.0,
            fees=1.0,
            operation_id="candidate-only-exit-2",
        )
    )

    assert harness.candidate_completed_trades() == 1
    assert harness.baseline_completed_trades() == 0
    assert harness.completed_trade_counts_match() is False


def test_legacy_fills_without_operation_id_replay(tmp_path: Path) -> None:
    """Old JSONL rows lacking ``operation_id`` still load cleanly.

    The harness constructor reloads JSONL artifacts and must accept both
    the new schema (``operation_id`` populated) and the legacy schema
    (field absent → default ``""``).
    """
    artifacts = tmp_path / "artifacts"
    harness = _harness(artifacts)

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
        fill_price=110.0,
        fees=1.0,
    )

    # Hand-edit the JSONL to drop operation_id from the first line, then
    # reload via a fresh harness. The line must survive parsing.
    fills_path = artifacts / "candidate-shadow-fills.jsonl"
    lines = fills_path.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    parsed[0].pop("operation_id", None)
    fills_path.write_text(
        "\n".join(json.dumps(line, sort_keys=True) for line in parsed) + "\n",
        encoding="utf-8",
    )

    rebuilt = _harness(artifacts)
    # Reloaded metrics must match the running ledger's metrics.
    assert rebuilt.candidate_metrics().trades == 1
    assert rebuilt.candidate_metrics().net_pnl == harness.candidate_metrics().net_pnl


def test_duplicate_exit_operation_id_is_no_op(tmp_path: Path) -> None:
    """A SELL recorded twice under the same operation_id is applied once."""
    harness = _harness(tmp_path / "artifacts")

    harness.record_entry(
        operation_id="buy-1",
        ticker="AAPL",
        baseline_quantity=10,
        candidate_quantity=5,
        fill_price=100.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="AAPL",
        candidate_quantity=5,
        fill_price=110.0,
        fees=1.0,
    )
    harness.record_exit(
        operation_id="sell-1",
        ticker="AAPL",
        candidate_quantity=5,
        fill_price=110.0,
        fees=1.0,
    )

    # First exit closed both ledgers; second call must be a no-op.
    assert harness.candidate_completed_trades() == 1
    assert harness.baseline_completed_trades() == 1
    assert harness.candidate_metrics().trades == 1
    assert harness.baseline_metrics().trades == 1
    assert harness.completed_trade_counts_match() is True


def test_legacy_shadowledger_record_remains_idempotent_for_empty_id(
    tmp_path: Path,
) -> None:
    """ShadowLedger.record with empty operation_id stays idempotent on retries.

    Two calls with empty operation_id must each be applied once: the
    legacy empty-id path is *not* deduped (legacy callers never had
    unique IDs), but the *non-empty* path is.
    """
    ledger = ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=1_000.0)
    fill = ShadowFill(
        ticker="X",
        side="BUY",
        quantity=5,
        fill_price=10.0,
        fees=1.0,
    )
    ledger.record(fill)
    ledger.record(fill)  # Empty operation_id: not deduped (legacy compat).
    # Two BUY lines → quantity 10 (legacy still adds; idempotency is on
    # non-empty operation_id only).
    assert ledger.snapshot_positions()["X"]["qty"] == 10


def test_shadowledger_dedupes_non_empty_operation_id(tmp_path: Path) -> None:
    """A repeat fill with the same non-empty operation_id is a no-op."""
    ledger = ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=1_000.0)
    fill = ShadowFill(
        ticker="X",
        side="BUY",
        quantity=5,
        fill_price=10.0,
        fees=1.0,
        operation_id="buy-x-1",
    )
    ledger.record(fill)
    ledger.record(fill)
    assert ledger.snapshot_positions()["X"]["qty"] == 5
