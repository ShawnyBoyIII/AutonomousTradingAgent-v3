from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from datetime import datetime

from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState


class PortfolioLedger:
    def __init__(
        self,
        db_path: Path,
        busy_timeout_ms: int = 5_000,
        lock_retry_attempts: int = 3,
        lock_retry_delay_s: float = 0.05,
    ) -> None:
        self.db_path = db_path
        self.busy_timeout_ms = max(int(busy_timeout_ms), 0)
        self.lock_retry_attempts = max(int(lock_retry_attempts), 1)
        self.lock_retry_delay_s = max(float(lock_retry_delay_s), 0.0)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000.0)
        if self.busy_timeout_ms > 0:
            conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        return conn

    def _execute_write(self, statement: str, params: tuple) -> None:
        """Run a write statement with bounded retry on `database is locked`.

        Sets `PRAGMA busy_timeout` per connection so SQLite blocks for that
        long before raising. If it still raises, we retry up to
        `lock_retry_attempts` times with a small backoff so a scanner and
        manager running at the same instant don't crash each other.
        """
        last_exc: Exception | None = None
        for attempt in range(self.lock_retry_attempts):
            try:
                with self._connect() as conn:
                    conn.execute(statement, params)
                return
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if "locked" not in str(exc).lower():
                    raise
                # Sleep with tiny exponential backoff before retrying.
                if attempt + 1 < self.lock_retry_attempts:
                    time.sleep(self.lock_retry_delay_s * (2 ** attempt))
        # Exhausted retries — re-raise the last lock error.
        assert last_exc is not None
        raise last_exc

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    ticker TEXT,
                    side TEXT,
                    quantity INTEGER,
                    fill_price REAL,
                    fees REAL,
                    filled_at TEXT,
                    pnl REAL DEFAULT 0,
                    strategy_tag TEXT DEFAULT ''
                )
                """
            )
            # Migration: add pnl column to pre-existing orders tables.
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN pnl REAL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            # Migration: add strategy_tag column for trade attribution.
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN strategy_tag TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL
                )
                """
            )
            # V2.5: Kill switch table for emergency trading halt
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS kill_switch (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled BOOLEAN NOT NULL DEFAULT 0,
                    reason TEXT,
                    triggered_at TEXT,
                    triggered_by TEXT
                )
                """
            )
            # Initialize kill switch as disabled if not exists
            conn.execute(
                """
                INSERT OR IGNORE INTO kill_switch (id, enabled, reason, triggered_at, triggered_by)
                VALUES (1, 0, NULL, NULL, NULL)
                """
            )
            # V3: Equity history table for drawdown + VaR
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS equity_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    equity REAL NOT NULL,
                    cash REAL,
                    realized_pnl REAL,
                    unrealized_pnl REAL
                )
                """
            )
        
        # Set restrictive permissions on database file (user read/write only)
        import os
        try:
            if self.db_path.exists():
                os.chmod(self.db_path, 0o600)
        except OSError:
            # File may not exist yet or permissions cannot be changed
            pass

    def load_portfolio_state(self) -> PortfolioState | None:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM portfolio_state WHERE id = 1"
            ).fetchone()

        if row is None:
            return None

        return PortfolioState.model_validate_json(row[0])

    def ensure_portfolio_state(self, starting_cash: float = 10_000.0) -> PortfolioState:
        state = self.load_portfolio_state()
        if state is not None:
            return state

        state = PortfolioState(cash=starting_cash, equity=starting_cash)
        self.save_portfolio_state(state)
        return state

    def deposit(self, amount: float, timestamp: datetime | None = None) -> PortfolioState:
        """Add ``amount`` of cash to the ledger (a capital deposit).

        Increases both ``cash`` and ``equity`` by ``amount`` and persists the
        updated state plus an equity snapshot so the deposit shows up in
        drawdown / VaR history. ``amount`` may be negative for a withdrawal.
        """
        if amount == 0:
            return self.ensure_portfolio_state()
        self.initialize()
        state = self.ensure_portfolio_state()
        state.cash = round(state.cash + amount, 2)
        state.equity = round(state.equity + amount, 2)
        self.save_portfolio_state(state)
        self.record_equity_snapshot(state, timestamp=timestamp)
        return state

    def save_portfolio_state(self, state: PortfolioState) -> None:
        self.initialize()
        self._execute_write(
            """
            INSERT INTO portfolio_state (id, payload)
            VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
            """,
            (state.model_dump_json(),),
        )

    def record_fill(
        self,
        fill: FillResult,
        side: str,
        realized_pnl: float = 0.0,
        strategy_tag: str = "",
    ) -> None:
        self.initialize()
        self._execute_write(
            """
            INSERT INTO orders (id, ticker, side, quantity, fill_price, fees, filled_at, pnl, strategy_tag)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.order_id,
                fill.ticker,
                side,
                fill.quantity,
                fill.fill_price,
                fill.fees,
                fill.filled_at.isoformat(),
                float(realized_pnl),
                strategy_tag,
            ),
        )

    def list_order_rows(self) -> list[dict[str, object]]:
        self.initialize()
        with self._connect() as conn:
            table_info = conn.execute("PRAGMA table_info(orders)").fetchall()
            has_tag = any(r[1] == "strategy_tag" for r in table_info)
            cols = "id, ticker, side, quantity, fill_price, fees, filled_at, pnl"
            if has_tag:
                cols += ", strategy_tag"
            rows = conn.execute(
                f"SELECT {cols} FROM orders ORDER BY filled_at ASC, id ASC"
            ).fetchall()

        results = []
        for row in rows:
            d: dict[str, object] = {
                "id": row[0],
                "ticker": row[1],
                "side": row[2],
                "quantity": row[3],
                "fill_price": row[4],
                "fees": row[5],
                "filled_at": row[6],
                "pnl": row[7] if row[7] is not None else 0.0,
            }
            tag = (row[8] if len(row) > 8 and row[8] else "") if has_tag else ""
            if tag:
                d["strategy_tag"] = tag
            results.append(d)
        return results

    def get_consecutive_losses(self) -> int:
        """Count consecutive SELL losses from most recent order backward.

        Looks at the ``orders`` table and counts how many recent SELL trades
        had ``pnl <= 0``. Stops at the first winning SELL.
        """
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT pnl FROM orders
                WHERE side = 'SELL' AND pnl IS NOT NULL
                ORDER BY filled_at DESC
                """
            ).fetchall()

        count = 0
        for (pnl,) in rows:
            if pnl is not None and pnl < 0:
                count += 1
            else:
                break
        return count

    def get_kill_switch_state(self) -> "KillSwitchState":
        """Get current kill switch state."""
        from trading_bot.safety.kill_switch import KillSwitchState

        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT enabled, reason, triggered_at, triggered_by FROM kill_switch WHERE id = 1"
            ).fetchone()

        if row is None:
            return KillSwitchState(enabled=False, reason=None, triggered_at=None, triggered_by=None)

        return KillSwitchState(
            enabled=bool(row[0]),
            reason=row[1],
            triggered_at=datetime.fromisoformat(row[2]) if row[2] else None,
            triggered_by=row[3],
        )

    def set_kill_switch(
        self,
        enabled: bool,
        reason: str | None,
        triggered_by: str = "system",
    ) -> None:
        """Set kill switch state."""
        self.initialize()
        now = datetime.now().isoformat()

        self._execute_write(
            """
            INSERT INTO kill_switch (id, enabled, reason, triggered_at, triggered_by)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                enabled = excluded.enabled,
                reason = excluded.reason,
                triggered_at = excluded.triggered_at,
                triggered_by = excluded.triggered_by
            """,
            (
                1 if enabled else 0,
                reason,
                now if enabled else None,
                triggered_by if enabled else None,
            ),
        )

    def record_equity_snapshot(
        self,
        state: PortfolioState,
        timestamp: datetime | None = None,
    ) -> None:
        """Append an equity snapshot to the history table.

        Called after each `save_portfolio_state` to build a time-series
        of equity, cash, and PnL for drawdown and VaR calculations.
        """
        self.initialize()
        ts = (timestamp or datetime.now()).isoformat()
        self._execute_write(
            """
            INSERT INTO equity_history (timestamp, equity, cash, realized_pnl, unrealized_pnl)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ts,
                float(state.equity),
                float(state.cash),
                float(state.realized_pnl),
                float(state.unrealized_pnl),
            ),
        )

    def list_equity_history(
        self,
        limit: int = 500,
    ) -> list[dict[str, object]]:
        """Return equity history rows (chronological order).

        Each row has: timestamp, equity, cash, realized_pnl, unrealized_pnl.
        """
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, equity, cash, realized_pnl, unrealized_pnl
                FROM equity_history
                ORDER BY id ASC
                LIMIT ?
                """,
                (max(limit, 1),),
            ).fetchall()

        return [
            {
                "timestamp": row[0],
                "equity": row[1],
                "cash": row[2],
                "realized_pnl": row[3] if row[3] is not None else 0.0,
                "unrealized_pnl": row[4] if row[4] is not None else 0.0,
            }
            for row in rows
        ]

    def list_recent_equity_history(
        self,
        limit: int | None = 500,
        since: datetime | None = None,
        naive_timezone: str | None = None,
    ) -> list[dict[str, object]]:
        """Return the newest equity snapshots in chronological order.

        ``limit`` caps the result set (``None`` ⇒ unbounded).
        ``since`` filters by UTC timestamp boundary. Legacy naive rows
        are interpreted in ``naive_timezone`` (defaults to UTC) so the
        cohort boundary is honored without rewriting history.
        """
        from trading_bot.analytics.evaluation_windows import normalize_timestamp

        normalized_since = (
            normalize_timestamp(since, naive_timezone) if since is not None else None
        )

        self.initialize()
        with self._connect() as conn:
            if limit is not None:
                rows = conn.execute(
                    """
                    SELECT timestamp, equity, cash, realized_pnl, unrealized_pnl
                    FROM equity_history
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (max(limit, 1),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT timestamp, equity, cash, realized_pnl, unrealized_pnl
                    FROM equity_history
                    ORDER BY id DESC
                    """
                ).fetchall()

        chronological = list(reversed(rows))
        if normalized_since is None:
            return [
                {
                    "timestamp": row[0],
                    "equity": row[1],
                    "cash": row[2],
                    "realized_pnl": row[3] if row[3] is not None else 0.0,
                    "unrealized_pnl": row[4] if row[4] is not None else 0.0,
                }
                for row in chronological
            ]

        filtered: list[tuple[datetime, sqlite3.Row | tuple]] = []
        for row in chronological:
            ts = row[0]
            ts_normalized = normalize_timestamp(ts, naive_timezone)
            if ts_normalized is None:
                continue
            if ts_normalized >= normalized_since:
                filtered.append((ts_normalized, row))

        filtered.sort(key=lambda pair: pair[0])
        return [
            {
                "timestamp": row[0],
                "equity": row[1],
                "cash": row[2],
                "realized_pnl": row[3] if row[3] is not None else 0.0,
                "unrealized_pnl": row[4] if row[4] is not None else 0.0,
            }
            for _, row in filtered
        ]
