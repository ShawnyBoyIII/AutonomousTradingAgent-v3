from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState
from trading_bot.portfolio.ledger import PortfolioLedger


def _sample_fill(order_id: str = "order-1") -> FillResult:
    return FillResult(
        order_id=order_id,
        ticker="AAPL",
        quantity=1,
        fill_price=100.0,
        fees=1.0,
        filled_at=datetime(2026, 6, 18, 10, 0, 0),
    )


class _FlakyConn:
    """Mock SQLite connection that locks N times then succeeds.

    Counts only execute() calls whose SQL contains `match_sql` so the
    ledger's internal CREATE TABLE statements don't pollute the lock
    counter.
    """

    def __init__(self, lock_n_times: int, match_sql: str) -> None:
        self.lock_n_times = lock_n_times
        self.match_sql = match_sql
        self.matched_calls = 0
        self.total_calls = 0

    def execute(self, sql, *args, **kwargs):
        self.total_calls += 1
        if self.match_sql in str(sql):
            self.matched_calls += 1
            if self.matched_calls <= self.lock_n_times:
                raise sqlite3.OperationalError("database is locked")
        return MagicMock()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_ledger_retries_on_locked_database(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(
        db_path,
        busy_timeout_ms=0,
        lock_retry_attempts=3,
        lock_retry_delay_s=0.0,
    )
    ledger.initialize()

    flaky = _FlakyConn(lock_n_times=2, match_sql="INSERT INTO orders")
    monkeypatch.setattr(ledger, "_connect", lambda: flaky)

    ledger.record_fill(_sample_fill(), side="BUY")

    # 2 flaky attempts + 1 success = 3 matched calls.
    assert flaky.matched_calls == 3


def test_ledger_raises_when_retries_exhausted(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(
        db_path,
        busy_timeout_ms=0,
        lock_retry_attempts=2,
        lock_retry_delay_s=0.0,
    )
    ledger.initialize()

    flaky = _FlakyConn(lock_n_times=10, match_sql="INSERT INTO orders")
    monkeypatch.setattr(ledger, "_connect", lambda: flaky)

    with pytest.raises(sqlite3.OperationalError):
        ledger.record_fill(_sample_fill(), side="BUY")

    assert flaky.matched_calls == 2


def test_ledger_does_not_retry_on_non_lock_errors(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(
        db_path,
        busy_timeout_ms=0,
        lock_retry_attempts=5,
        lock_retry_delay_s=0.0,
    )
    ledger.initialize()

    insert_calls = {"n": 0}

    class _NonLockConn(_FlakyConn):
        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO orders" in str(sql):
                insert_calls["n"] += 1
                raise sqlite3.OperationalError("no such table: orders")
            return MagicMock()

    non_lock_conn = _NonLockConn(lock_n_times=0, match_sql="INSERT INTO orders")
    monkeypatch.setattr(ledger, "_connect", lambda: non_lock_conn)

    with pytest.raises(sqlite3.OperationalError):
        ledger.record_fill(_sample_fill(), side="BUY")

    assert insert_calls["n"] == 1


def test_ledger_save_portfolio_state_retries_on_lock(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(
        db_path,
        busy_timeout_ms=0,
        lock_retry_attempts=3,
        lock_retry_delay_s=0.0,
    )
    ledger.initialize()

    flaky = _FlakyConn(lock_n_times=1, match_sql="INSERT INTO portfolio_state")
    monkeypatch.setattr(ledger, "_connect", lambda: flaky)

    ledger.save_portfolio_state(PortfolioState(cash=10_000.0, equity=10_000.0))

    # 1 flaky + 1 success = 2 matched calls.
    assert flaky.matched_calls == 2


def test_ledger_busy_timeout_pragma_is_applied_on_its_own_connection(tmp_path: Path) -> None:
    """PRAGMA busy_timeout is per-connection; assert via the ledger's _connect."""
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path, busy_timeout_ms=7500)
    ledger.initialize()

    with ledger._connect() as conn:
        result = conn.execute("PRAGMA busy_timeout").fetchone()

    assert result is not None
    assert result[0] == 7500


def test_ledger_default_busy_timeout_is_5000_ms(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path)
    ledger.initialize()

    with ledger._connect() as conn:
        result = conn.execute("PRAGMA busy_timeout").fetchone()

    assert result is not None
    assert result[0] == 5000
