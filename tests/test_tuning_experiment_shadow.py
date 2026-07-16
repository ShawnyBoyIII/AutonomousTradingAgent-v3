from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from trading_bot.learning.experiments.shadow import ShadowFill, ShadowLedger

if TYPE_CHECKING:
    pass


def test_shadow_ledger_matches_broker_with_identical_fills(tmp_path: Path) -> None:
    ledger = ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=100_000.0)
    fill = ShadowFill(ticker="AAPL", side="BUY", quantity=1, fill_price=10.0, fees=1.0)

    ledger.record(fill)
    metrics = ledger.metrics()

    assert metrics.trades == 1
    assert metrics.net_pnl == -11.0


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


def test_maybe_record_shadow_fill_records_buy_when_shadow_active() -> None:
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
    assert metrics.trades == 1
    assert metrics.net_pnl == -501.5  # -(5 * 100 + 1.5)


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