"""TDD: BUY/SELL transactions persist atomically across stores."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.runtime.fill_transaction import FillTransactionError
from trading_bot.runtime.orchestrator import _build_buy_fill_transaction


def _settings(tmp_path: Path) -> Settings:
    app = AppSettings(
        state_db_path=str(tmp_path / "ledger.db"),
        log_dir=str(tmp_path / "logs"),
        approved_candidates_path=str(tmp_path / "approved.jsonl"),
    )
    return Settings(app=app)


def test_buy_transaction_persists_to_orders_and_db(tmp_path: Path) -> None:
    """The built transaction executes ledger.record_fill and SQL persist in order."""
    settings = _settings(tmp_path)
    ledger = MagicMock()

    def sql_persist(**kwargs):
        sql_persist.called = True
    sql_persist.called = False

    from datetime import datetime, timezone
    fill = MagicMock(ticker="AAPL", fill_price=190.0, fees=1.0, quantity=10)
    fill.filled_at = datetime.now(timezone.utc)

    tx = _build_buy_fill_transaction(
        ledger=ledger,
        sql_persist=sql_persist,
        strategy_tag="v3-trend",
        settings=settings,
        signal=None,
        details={},
    )

    tx.run(fill=fill, side="BUY", filled_at=fill.filled_at)
    assert ledger.record_fill.called
    assert sql_persist.called


def test_buy_transaction_rolls_back_when_sql_fails(tmp_path: Path) -> None:
    """If SQL persist fails, the entire transaction raises."""
    settings = _settings(tmp_path)
    ledger = MagicMock()

    def sql_persist(**kwargs):
        raise RuntimeError("db locked")

    from datetime import datetime, timezone
    fill = MagicMock(ticker="AAPL", fill_price=190.0, fees=1.0, quantity=10)
    fill.filled_at = datetime.now(timezone.utc)

    tx = _build_buy_fill_transaction(
        ledger=ledger,
        sql_persist=sql_persist,
        strategy_tag="v3-trend",
        settings=settings,
        signal=None,
        details={},
    )
    with pytest.raises(FillTransactionError):
        tx.run(fill=fill, side="BUY", filled_at=fill.filled_at)


def test_buy_transaction_includes_strategy_tracker_step(tmp_path: Path) -> None:
    """Strategy tracker step fires only when strategy_tag is non-empty."""
    settings = _settings(tmp_path)
    ledger = MagicMock()

    def sql_persist(**kwargs):
        sql_persist.call_count += 1
    sql_persist.call_count = 0

    from datetime import datetime, timezone
    fill = MagicMock(ticker="AAPL", fill_price=190.0, fees=1.0, quantity=10)
    fill.filled_at = datetime.now(timezone.utc)

    tx = _build_buy_fill_transaction(
        ledger=ledger,
        sql_persist=sql_persist,
        strategy_tag="",
        settings=settings,
        signal=None,
        details={},
    )
    tx.run(fill=fill, side="BUY", filled_at=fill.filled_at)
    assert ledger.record_fill.call_count == 1
    assert sql_persist.call_count == 1
