from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trading_bot.learning.experiments.shadow import ShadowFill, ShadowLedger

if TYPE_CHECKING:
    pass


def test_shadow_ledger_records_no_trade_until_round_trip(tmp_path: Path) -> None:
    """A BUY alone must not count as a closed trade; trades only counts
    SELLs that close an open position so PF/drawdown metrics remain valid.
    """
    ledger = ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=100_000.0)
    fill = ShadowFill(ticker="AAPL", side="BUY", quantity=1, fill_price=10.0, fees=1.0)

    ledger.record(fill)
    metrics = ledger.metrics()

    # Open position: not yet a closed trade.
    assert metrics.trades == 0
    assert metrics.net_pnl == 0.0


def test_shadow_ledger_closes_round_trip(tmp_path: Path) -> None:
    ledger = ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=100_000.0)
    ledger.record(ShadowFill(ticker="AAPL", side="BUY", quantity=1, fill_price=10.0, fees=1.0))
    ledger.record(ShadowFill(ticker="AAPL", side="SELL", quantity=1, fill_price=11.0, fees=1.0))

    metrics = ledger.metrics()
    assert metrics.trades == 1
    # Bought at 10 with 1 fee, sold at 11 with 1 fee → realized = (11-10)*1 - 2 = -1
    assert abs(metrics.net_pnl - (-1.0)) < 0.001


def test_shadow_ledger_does_not_touch_burn_in_db(tmp_path: Path) -> None:
    burn_in_db = tmp_path / "burn_in.db"
    burn_in_db.touch()
    assert burn_in_db.exists()
    initial_size = burn_in_db.stat().st_size

    ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=10_000.0).record(
        ShadowFill(ticker="X", side="BUY", quantity=1, fill_price=10.0, fees=1.0)
    )

    assert burn_in_db.exists()
    assert burn_in_db.stat().st_size == initial_size


def test_shadow_ledger_appends_all_fills_to_jsonl(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "shadow"
    ledger = ShadowLedger(artifacts_dir=artifacts_dir, starting_cash=100_000.0)
    fills = [
        ShadowFill(ticker="AAPL", side="BUY", quantity=1, fill_price=10.0, fees=1.0),
        ShadowFill(ticker="MSFT", side="BUY", quantity=2, fill_price=20.0, fees=1.0),
        ShadowFill(ticker="NVDA", side="BUY", quantity=3, fill_price=30.0, fees=1.0),
    ]
    for fill in fills:
        ledger.record(fill)

    fills_path = artifacts_dir / "shadow-fills.jsonl"
    equity_path = artifacts_dir / "shadow-equity.jsonl"
    assert fills_path.exists()
    assert equity_path.exists()

    fill_lines = fills_path.read_text(encoding="utf-8").splitlines()
    equity_lines = equity_path.read_text(encoding="utf-8").splitlines()
    assert len(fill_lines) == 3
    assert len(equity_lines) == 3

    import json

    decoded_fills = [json.loads(line) for line in fill_lines]
    assert [entry["ticker"] for entry in decoded_fills] == ["AAPL", "MSFT", "NVDA"]


def test_maybe_record_shadow_fill_records_buy_when_shadow_active() -> None:
    """A standalone BUY records the fill but does NOT count as a closed
    trade until a matching SELL closes it."""
    from trading_bot.runtime.orchestrator import _maybe_record_shadow_fill

    ledger = ShadowLedger(artifacts_dir=Path("/tmp/nonexistent-shadow-test"), starting_cash=50_000.0)

    _maybe_record_shadow_fill(
        candidate_fill={
            "ticker": "AAPL",
            "side": "BUY",
            "quantity": 5,
            "fill_price": 100.0,
            "fees": 1.5,
        },
        baseline_signal={},
        shadow=ledger,
    )

    metrics = ledger.metrics()
    assert metrics.trades == 0
    assert metrics.net_pnl == 0.0


def test_maybe_record_shadow_fill_noop_when_shadow_none() -> None:
    from trading_bot.runtime.orchestrator import _maybe_record_shadow_fill

    _maybe_record_shadow_fill(
        candidate_fill={
            "ticker": "AAPL",
            "side": "BUY",
            "quantity": 5,
            "fill_price": 100.0,
            "fees": 1.5,
        },
        baseline_signal={},
        shadow=None,
    )


def test_maybe_record_shadow_fill_skips_sell_side() -> None:
    from trading_bot.runtime.orchestrator import _maybe_record_shadow_fill

    ledger = ShadowLedger(artifacts_dir=Path("/tmp/nonexistent-shadow-test"), starting_cash=50_000.0)

    _maybe_record_shadow_fill(
        candidate_fill={
            "ticker": "AAPL",
            "side": "SELL",
            "quantity": 5,
            "fill_price": 110.0,
            "fees": 1.5,
        },
        baseline_signal={},
        shadow=ledger,
    )

    assert ledger.metrics().net_pnl == 0.0