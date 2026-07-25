from datetime import datetime, timezone
import sqlite3

from trading_bot.models.order import FillResult
from trading_bot.portfolio.ledger import PortfolioLedger


def test_initialize_migrates_existing_orders_table(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE orders (
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

    ledger = PortfolioLedger(db_path)
    ledger.initialize()
    ledger.initialize()

    with sqlite3.connect(db_path) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(orders)")]

    assert columns[-2:] == [
        "canary_experiment_id",
        "canary_baseline_quantity",
    ]


def test_record_fill_persists_canary_metadata(tmp_path):
    ledger = PortfolioLedger(tmp_path / "state.db")
    fill = FillResult(
        order_id="buy-1",
        ticker="AAPL",
        quantity=5,
        fill_price=100.0,
        fees=1.0,
        filled_at=datetime.now(timezone.utc),
    )

    ledger.record_fill(
        fill,
        "BUY",
        canary_experiment_id="exp-1",
        canary_baseline_quantity=10,
    )

    assert ledger.list_canary_order_rows("exp-1") == [
        {
            "id": "buy-1",
            "ticker": "AAPL",
            "side": "BUY",
            "quantity": 5,
            "fill_price": 100.0,
            "fees": 1.0,
            "filled_at": fill.filled_at.isoformat(),
            "canary_baseline_quantity": 10,
        }
    ]
