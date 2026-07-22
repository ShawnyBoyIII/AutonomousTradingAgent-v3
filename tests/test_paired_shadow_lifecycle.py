import json
from pathlib import Path

from trading_bot.learning.experiments.shadow import (
    PairedShadowHarness,
    ShadowFill,
    ShadowLedger,
)
from trading_bot.learning.experiments.models import ParameterChange


def test_shadow_ledger_records_baseline_buys_and_sells(tmp_path: Path) -> None:
    ledger = ShadowLedger(tmp_path / "artifacts", starting_cash=10_000.0)

    ledger.record(ShadowFill(ticker="SPY", side="BUY", quantity=10, fill_price=100.0, fees=1.0))
    ledger.record(ShadowFill(ticker="SPY", side="SELL", quantity=10, fill_price=101.0, fees=1.0))

    metrics = ledger.metrics()
    assert metrics.trades == 1
    # Sold at 101 with 1 fee → 1010 - 1 = 1009 proceeds. Bought at 100 with 1 fee → 1001 cost. Net = 8.
    assert abs(metrics.net_pnl - 8.0) < 0.001
    assert metrics.profit_factor > 0  # Only a win was closed; PF is finite


def test_shadow_ledger_profit_factor_two_wins_one_loss(tmp_path: Path) -> None:
    ledger = ShadowLedger(tmp_path / "artifacts", starting_cash=10_000.0)
    # Trade 1: BUY 10@100, SELL 10@110 → +98 win
    ledger.record(ShadowFill(ticker="A", side="BUY", quantity=10, fill_price=100.0, fees=1.0))
    ledger.record(ShadowFill(ticker="A", side="SELL", quantity=10, fill_price=110.0, fees=1.0))
    # Trade 2: BUY 10@100, SELL 10@95 → -52 loss
    ledger.record(ShadowFill(ticker="B", side="BUY", quantity=10, fill_price=100.0, fees=1.0))
    ledger.record(ShadowFill(ticker="B", side="SELL", quantity=10, fill_price=95.0, fees=1.0))
    # Trade 3: BUY 10@100, SELL 10@108 → +78 win
    ledger.record(ShadowFill(ticker="C", side="BUY", quantity=10, fill_price=100.0, fees=1.0))
    ledger.record(ShadowFill(ticker="C", side="SELL", quantity=10, fill_price=108.0, fees=1.0))

    m = ledger.metrics()
    assert m.trades == 3
    # gross_profit = 98 + 78 = 176, gross_loss = 52 → PF = 176/52 ≈ 3.385
    assert abs(m.profit_factor - (176.0 / 52.0)) < 0.01
    assert abs(m.net_pnl - (98.0 - 52.0 + 78.0)) < 0.01


def test_shadow_ledger_reloads_state_from_artifacts(tmp_path: Path) -> None:
    """A fresh ShadowLedger on the same artifacts dir must reproduce the
    closed-trade P&L and equity curve so canary metrics survive a restart."""
    artifacts = tmp_path / "artifacts"
    ledger_a = ShadowLedger(artifacts, starting_cash=10_000.0)
    # Trade 1: WIN
    ledger_a.record(ShadowFill(ticker="SPY", side="BUY", quantity=10, fill_price=100.0, fees=1.0))
    ledger_a.record(ShadowFill(ticker="SPY", side="SELL", quantity=10, fill_price=110.0, fees=1.0))
    # Trade 2: LOSS (so profit_factor is finite and comparable)
    ledger_a.record(ShadowFill(ticker="QQQ", side="BUY", quantity=10, fill_price=100.0, fees=1.0))
    ledger_a.record(ShadowFill(ticker="QQQ", side="SELL", quantity=10, fill_price=95.0, fees=1.0))
    metrics_a = ledger_a.metrics()

    ledger_b = ShadowLedger(artifacts, starting_cash=10_000.0)
    metrics_b = ledger_b.metrics()
    assert metrics_b.trades == metrics_a.trades == 2
    assert abs(metrics_b.net_pnl - metrics_a.net_pnl) < 0.001
    assert abs(metrics_b.profit_factor - metrics_a.profit_factor) < 0.001
    assert abs(metrics_b.max_drawdown_pct - metrics_a.max_drawdown_pct) < 0.001


def test_paired_shadow_routes_each_fill_at_independent_size(tmp_path: Path) -> None:
    """PairedShadowHarness records the same logical fill into two ledgers
    at *different* quantities — baseline at 1.0x and candidate at the
    configured candidate multiplier (0.5 here). The two ledgers must
    produce different closed-trade P&L so the canary gate compares
    apples-to-apples economics, not the same fill twice.
    """
    artifacts = tmp_path / "artifacts"
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )
    harness = PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=change,
        baseline_multiplier=1.0,
        candidate_multiplier=0.5,
    )

    harness.record_paired("SPY", "BUY", raw_quantity=10, fill_price=100.0, fees=1.0)
    harness.record_paired("SPY", "SELL", raw_quantity=10, fill_price=110.0, fees=1.0)

    candidate_metrics = harness.candidate.metrics()
    baseline_metrics = harness.baseline.metrics()

    # Both ledgers record one closed trade.
    assert baseline_metrics.trades == 1
    assert candidate_metrics.trades == 1
    # Candidate sized at 0.5x → realized half the win.
    # Baseline: BUY 10@100 fee 1 + SELL 10@110 fee 1 = 1100-1-(1000+1) = 98
    # Candidate: BUY 5@100 fee 1 + SELL 5@110 fee 1 = 550-1-(500+1) = 48
    assert abs(baseline_metrics.net_pnl - 98.0) < 0.01
    assert abs(candidate_metrics.net_pnl - 48.0) < 0.01
    assert (artifacts / "shadow-fills.jsonl").exists()
    assert (artifacts / "candidate-shadow-fills.jsonl").exists()


def test_paired_shadow_baseline_records_equity_curves(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )
    harness = PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=change,
        baseline_multiplier=1.0,
        candidate_multiplier=0.5,
    )

    harness.record_paired("SPY", "BUY", raw_quantity=10, fill_price=100.0, fees=1.0)
    harness.record_paired("SPY", "SELL", raw_quantity=10, fill_price=110.0, fees=1.0)

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
    # Baseline ends at 10000 + 98 = 10098 (approx)
    baseline_last = baseline_equity[-1]
    assert abs(baseline_last - 10_098.0) < 1.0
    # Candidate ends at 10000 + 48 = 10048 (approx)
    candidate_last = candidate_equity[-1]
    assert abs(candidate_last - 10_048.0) < 1.0


def test_paired_shadow_handles_zero_or_negative_quantities(tmp_path: Path) -> None:
    """Harness must ignore zero/negative raw_quantity (defensive against
    upstream partial-fill anomalies)."""
    artifacts = tmp_path / "artifacts"
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )
    harness = PairedShadowHarness(
        artifacts_dir=artifacts,
        starting_cash=10_000.0,
        change=change,
    )

    harness.record_paired("SPY", "BUY", raw_quantity=0, fill_price=100.0, fees=0.0)
    harness.record_paired("SPY", "BUY", raw_quantity=-1, fill_price=100.0, fees=0.0)

    assert harness.baseline.metrics().trades == 0
    assert harness.candidate.metrics().trades == 0
