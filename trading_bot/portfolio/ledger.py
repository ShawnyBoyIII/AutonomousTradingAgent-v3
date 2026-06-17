from __future__ import annotations

import sqlite3
from pathlib import Path

from trading_bot.models.order import FillResult
from trading_bot.models.portfolio import PortfolioState


class PortfolioLedger:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id TEXT PRIMARY KEY,
                    ticker TEXT,
                    side TEXT,
                    quantity INTEGER,
                    fill_price REAL,
                    fees REAL,
                    filled_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL
                )
                """
            )

    def load_portfolio_state(self) -> PortfolioState | None:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
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

    def save_portfolio_state(self, state: PortfolioState) -> None:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO portfolio_state (id, payload)
                VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload
                """,
                (state.model_dump_json(),),
            )

    def record_fill(self, fill: FillResult, side: str) -> None:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO orders (id, ticker, side, quantity, fill_price, fees, filled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.order_id,
                    fill.ticker,
                    side,
                    fill.quantity,
                    fill.fill_price,
                    fill.fees,
                    fill.filled_at.isoformat(),
                ),
            )

    def list_order_rows(self) -> list[dict[str, object]]:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, ticker, side, quantity, fill_price, fees, filled_at
                FROM orders
                ORDER BY filled_at ASC, id ASC
                """
            ).fetchall()

        return [
            {
                "id": row[0],
                "ticker": row[1],
                "side": row[2],
                "quantity": row[3],
                "fill_price": row[4],
                "fees": row[5],
                "filled_at": row[6],
            }
            for row in rows
        ]
