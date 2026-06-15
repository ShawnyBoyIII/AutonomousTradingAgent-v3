from __future__ import annotations

import sqlite3
from pathlib import Path


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
