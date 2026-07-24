from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pytest

from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState, Position
from trading_bot.portfolio.ledger import PortfolioLedger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> PortfolioLedger:
    return PortfolioLedger(tmp_path / "test.db")


def _fill(
    order_id: str = "ord-1",
    ticker: str = "AAPL",
    quantity: int = 10,
    fill_price: float = 150.0,
    fees: float = 1.0,
    filled_at: datetime | None = None,
) -> FillResult:
    return FillResult(
        order_id=order_id,
        ticker=ticker,
        quantity=quantity,
        fill_price=fill_price,
        fees=fees,
        filled_at=filled_at or datetime(2025, 1, 1, 9, 30),
    )


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


def test_initialize_creates_tables(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "sub" / "test.db")
    ledger.initialize()
    assert ledger.db_path.exists()

    with sqlite3.connect(ledger.db_path) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    assert {"orders", "portfolio_state", "kill_switch", "equity_history"} <= names


def test_initialize_is_idempotent(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "test.db")
    ledger.initialize()
    ledger.initialize()  # should not raise


def test_initialize_creates_parent_dir(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "nested" / "deep" / "test.db")
    ledger.initialize()
    assert ledger.db_path.parent.exists()


# ---------------------------------------------------------------------------
# ensure_portfolio_state / load / save
# ---------------------------------------------------------------------------


def test_load_portfolio_state_returns_none_when_empty(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "test.db")
    assert ledger.load_portfolio_state() is None


def test_ensure_portfolio_state_creates_default(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "test.db")
    state = ledger.ensure_portfolio_state(starting_cash=25_000.0)
    assert state.cash == 25_000.0
    assert state.equity == 25_000.0
    assert state.positions == {}


def test_ensure_portfolio_state_returns_existing(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "test.db")
    ledger.ensure_portfolio_state(starting_cash=10_000.0)

    state = ledger.ensure_portfolio_state(starting_cash=99_999.0)
    assert state.cash == 10_000.0


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "test.db")
    state = PortfolioState(
        cash=8_000.0,
        equity=12_000.0,
        positions={"AAPL": Position(ticker="AAPL", quantity=10, average_cost=150.0)},
        realized_pnl=100.0,
        unrealized_pnl=-50.0,
    )
    ledger.save_portfolio_state(state)
    loaded = ledger.load_portfolio_state()
    assert loaded is not None
    assert loaded.cash == 8_000.0
    assert loaded.equity == 12_000.0
    assert loaded.realized_pnl == 100.0
    assert loaded.unrealized_pnl == -50.0
    assert "AAPL" in loaded.positions


def test_save_portfolio_state_upsert_overwrites(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "test.db")
    ledger.save_portfolio_state(PortfolioState(cash=1_000.0, equity=1_000.0))
    ledger.save_portfolio_state(PortfolioState(cash=2_000.0, equity=2_000.0))

    loaded = ledger.load_portfolio_state()
    assert loaded is not None
    assert loaded.cash == 2_000.0


def test_load_initializes_db(tmp_path: Path) -> None:
    ledger = PortfolioLedger(tmp_path / "fresh.db")
    assert not ledger.db_path.exists()
    result = ledger.load_portfolio_state()
    assert result is None
    assert ledger.db_path.exists()


# ---------------------------------------------------------------------------
# record_fill / list_order_rows
# ---------------------------------------------------------------------------


def test_record_fill_inserts_row(ledger: PortfolioLedger) -> None:
    ledger.record_fill(_fill(), side="BUY")
    rows = ledger.list_order_rows()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["side"] == "BUY"
    assert rows[0]["pnl"] == 0.0


def test_record_fill_stores_realized_pnl(ledger: PortfolioLedger) -> None:
    ledger.record_fill(_fill(order_id="ord-sell"), side="SELL", realized_pnl=42.5)
    rows = ledger.list_order_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == "ord-sell"
    assert rows[0]["side"] == "SELL"
    assert rows[0]["pnl"] == 42.5



def test_list_order_rows_returns_chronological(ledger: PortfolioLedger) -> None:
    ledger.record_fill(_fill(order_id="a", filled_at=datetime(2025, 1, 1, 9, 30)), side="BUY")
    ledger.record_fill(_fill(order_id="b", filled_at=datetime(2025, 1, 1, 9, 31)), side="BUY")
    rows = ledger.list_order_rows()
    assert [r["id"] for r in rows] == ["a", "b"]


def test_list_order_rows_empty_returns_empty_list(ledger: PortfolioLedger) -> None:
    assert ledger.list_order_rows() == []


def test_list_recent_order_rows_returns_newest_first_with_deterministic_ties(
    ledger: PortfolioLedger,
) -> None:
    same_time = datetime(2025, 1, 1, 9, 31)
    ledger.record_fill(_fill(order_id="older", filled_at=datetime(2025, 1, 1, 9, 30)), side="BUY")
    ledger.record_fill(_fill(order_id="tie-a", filled_at=same_time), side="BUY")
    ledger.record_fill(_fill(order_id="tie-b", filled_at=same_time), side="SELL")

    rows = ledger.list_recent_order_rows(limit=2)

    assert [row["id"] for row in rows] == ["tie-b", "tie-a"]


def test_list_recent_order_rows_clamps_limits(ledger: PortfolioLedger) -> None:
    ledger.initialize()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.executemany(
            """
            INSERT INTO orders
                (id, ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag)
            VALUES (?, 'AAPL', 'BUY', 1, 100.0, 0.0, ?, 0.0, '')
            """,
            [(f"order-{index:03d}", f"2025-01-01T09:{index // 60:02d}:{index % 60:02d}") for index in range(501)],
        )

    assert len(ledger.list_recent_order_rows(limit=0)) == 1
    assert len(ledger.list_recent_order_rows(limit=1_000)) == 500


def test_list_recent_order_rows_normalizes_timestamps_before_sql_limit(
    ledger: PortfolioLedger,
) -> None:
    ledger.initialize()
    rows = [
        ("legacy-newest", "2026-07-22T10:30:00"),
        ("aware-second", "2026-07-22T14:00:00+00:00"),
        ("aware-older", "2026-07-22T13:00:00+00:00"),
        ("malformed", "zzzz-not-a-timestamp"),
        ("missing", None),
    ]
    with sqlite3.connect(ledger.db_path) as conn:
        conn.executemany(
            """
            INSERT INTO orders
                (id, ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag)
            VALUES (?, 'AAPL', 'BUY', 1, 100.0, 0.0, ?, 0.0, '')
            """,
            rows,
        )

    recent = ledger.list_recent_order_rows(
        limit=2,
        naive_timezone="America/New_York",
    )

    assert [row["id"] for row in recent] == ["legacy-newest", "aware-second"]


def test_list_recent_order_rows_filters_timestamp_conversion_overflow(
    ledger: PortfolioLedger,
) -> None:
    ledger.initialize()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.executemany(
            """
            INSERT INTO orders
                (id, ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag)
            VALUES (?, 'AAPL', 'BUY', 1, 100.0, 0.0, ?, 0.0, '')
            """,
            [
                ("valid", "2026-07-22T14:00:00+00:00"),
                ("underflow", "0001-01-01T00:00:00+14:00"),
            ],
        )

    recent = ledger.list_recent_order_rows(limit=1, naive_timezone="UTC")

    assert [row["id"] for row in recent] == ["valid"]


# ---------------------------------------------------------------------------
# get_consecutive_losses
# ---------------------------------------------------------------------------


def test_consecutive_losses_none_when_no_sells(ledger: PortfolioLedger) -> None:
    ledger.record_fill(_fill(order_id="b1"), side="BUY")
    assert ledger.get_consecutive_losses() == 0


def test_consecutive_losses_counts_recent_negatives(ledger: PortfolioLedger) -> None:
    ledger.record_fill(_fill(order_id="w", filled_at=datetime(2025, 1, 1, 9, 30)), side="SELL", realized_pnl=10.0)
    ledger.record_fill(_fill(order_id="l1", filled_at=datetime(2025, 1, 1, 9, 31)), side="SELL", realized_pnl=-5.0)
    ledger.record_fill(_fill(order_id="l2", filled_at=datetime(2025, 1, 1, 9, 32)), side="SELL", realized_pnl=-3.0)
    # most recent first: l2(-), l1(-), w(+)
    assert ledger.get_consecutive_losses() == 2


def test_consecutive_losses_stops_at_zero(ledger: PortfolioLedger) -> None:
    ledger.record_fill(_fill(order_id="l1", filled_at=datetime(2025, 1, 1, 9, 30)), side="SELL", realized_pnl=-1.0)
    ledger.record_fill(_fill(order_id="z", filled_at=datetime(2025, 1, 1, 9, 31)), side="SELL", realized_pnl=0.0)
    # get_consecutive_losses breaks on pnl >= 0
    assert ledger.get_consecutive_losses() == 0


def test_consecutive_losses_ignores_buys(ledger: PortfolioLedger) -> None:
    ledger.record_fill(_fill(order_id="b1", filled_at=datetime(2025, 1, 1, 9, 30)), side="BUY", realized_pnl=-1.0)
    assert ledger.get_consecutive_losses() == 0


# ---------------------------------------------------------------------------
# equity snapshots
# ---------------------------------------------------------------------------


def test_record_equity_snapshot_inserts_row(ledger: PortfolioLedger) -> None:
    state = PortfolioState(cash=10_000.0, equity=10_500.0, realized_pnl=100.0, unrealized_pnl=50.0)
    ledger.record_equity_snapshot(state)
    rows = ledger.list_equity_history()
    assert len(rows) == 1
    assert rows[0]["equity"] == 10_500.0
    assert rows[0]["cash"] == 10_000.0
    assert rows[0]["realized_pnl"] == 100.0
    assert rows[0]["unrealized_pnl"] == 50.0


def test_record_equity_snapshot_uses_provided_timestamp(ledger: PortfolioLedger) -> None:
    state = PortfolioState(cash=5_000.0, equity=5_000.0)
    ts = datetime(2024, 6, 1, 10, 0)
    ledger.record_equity_snapshot(state, timestamp=ts)
    rows = ledger.list_equity_history()
    assert rows[0]["timestamp"] == ts.isoformat()


def test_list_equity_history_chronological(ledger: PortfolioLedger) -> None:
    for i in range(3):
        ledger.record_equity_snapshot(
            PortfolioState(cash=1_000.0, equity=1_000.0 + i),
            timestamp=datetime(2025, 1, 1, 9 + i),
        )
    rows = ledger.list_equity_history()
    equities = [r["equity"] for r in rows]
    assert equities == [1_000.0, 1_001.0, 1_002.0]


def test_list_equity_history_limit(ledger: PortfolioLedger) -> None:
    for i in range(5):
        ledger.record_equity_snapshot(
            PortfolioState(cash=1_000.0, equity=float(i)),
            timestamp=datetime(2025, 1, 1, 9, i),
        )
    rows = ledger.list_equity_history(limit=2)
    assert len(rows) == 2


def test_list_equity_history_limit_floor_one(ledger: PortfolioLedger) -> None:
    ledger.record_equity_snapshot(PortfolioState(cash=1.0, equity=1.0))
    rows = ledger.list_equity_history(limit=0)
    assert len(rows) == 1


def test_list_equity_history_empty(ledger: PortfolioLedger) -> None:
    assert ledger.list_equity_history() == []


# ---------------------------------------------------------------------------
# kill switch state
# ---------------------------------------------------------------------------


def test_get_kill_switch_state_defaults_disabled(ledger: PortfolioLedger) -> None:
    state = ledger.get_kill_switch_state()
    assert state.enabled is False
    assert state.reason is None
    assert state.triggered_at is None
    assert state.triggered_by is None


def test_set_kill_switch_halt_then_read(ledger: PortfolioLedger) -> None:
    ledger.set_kill_switch(enabled=True, reason="manual", triggered_by="op")
    state = ledger.get_kill_switch_state()
    assert state.enabled is True
    assert state.reason == "manual"
    assert state.triggered_by == "op"
    assert state.triggered_at is not None


def test_set_kill_switch_resume_clears_fields(ledger: PortfolioLedger) -> None:
    ledger.set_kill_switch(enabled=True, reason="x", triggered_by="y")
    ledger.set_kill_switch(enabled=False, reason=None, triggered_by="system")
    state = ledger.get_kill_switch_state()
    assert state.enabled is False
    assert state.triggered_at is None
    assert state.triggered_by is None


# ---------------------------------------------------------------------------
# constructor / retries
# ---------------------------------------------------------------------------


def test_constructor_clamps_negative_values(tmp_path: Path) -> None:
    ledger = PortfolioLedger(
        tmp_path / "test.db",
        busy_timeout_ms=-5,
        lock_retry_attempts=-2,
        lock_retry_delay_s=-0.1,
    )
    assert ledger.busy_timeout_ms == 0
    assert ledger.lock_retry_attempts == 1
    assert ledger.lock_retry_delay_s == 0.0


def test_lock_retry_succeeds_after_retries(ledger: PortfolioLedger) -> None:
    """Force a transient locked error once during _execute_write, then succeed."""
    calls = {"n": 0}
    real_connect = ledger._connect

    def flaky_connect():
        calls["n"] += 1
        # initialize() makes the first _connect call; fail the second one
        # (which is the first _execute_write attempt) so the retry kicks in.
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        return real_connect()

    ledger._connect = flaky_connect  # type: ignore[method-assign]
    ledger.save_portfolio_state(PortfolioState(cash=100.0, equity=100.0))
    assert calls["n"] >= 3


def test_lock_retry_exhausted_re_raises(ledger: PortfolioLedger) -> None:
    def always_locked(statement: str, params: tuple) -> None:
        raise sqlite3.OperationalError("database is locked")

    ledger._execute_write = always_locked  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        ledger.save_portfolio_state(PortfolioState(cash=100.0, equity=100.0))


def test_non_lock_error_propagates_immediately(ledger: PortfolioLedger) -> None:
    def raise_syntax(statement: str, params: tuple) -> None:
        raise sqlite3.OperationalError("syntax error")

    ledger._execute_write = raise_syntax  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError) as exc:
        ledger.save_portfolio_state(PortfolioState(cash=100.0, equity=100.0))
    assert "syntax" in str(exc.value)


def test_persistence_across_ledger_instances(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    ledger1 = PortfolioLedger(db_path)
    ledger1.save_portfolio_state(PortfolioState(cash=7_000.0, equity=7_000.0))

    ledger2 = PortfolioLedger(db_path)
    state = ledger2.load_portfolio_state()
    assert state is not None
    assert state.cash == 7_000.0
