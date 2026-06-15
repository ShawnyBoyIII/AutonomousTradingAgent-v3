from datetime import datetime
from pathlib import Path
import sqlite3

from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest
from trading_bot.portfolio.ledger import PortfolioLedger


def test_market_buy_updates_cash_and_position() -> None:
    broker = PaperBroker(starting_cash=10000, fee_per_order=1.0, slippage_bps=0)
    order = OrderRequest(
        ticker="AAPL",
        side="BUY",
        order_type="market",
        quantity=10,
        submitted_at=datetime.now(),
    )

    fill = broker.submit_order(order, market_price=100.0)

    assert fill.fill_price == 100.0
    assert broker.cash == 8999.0
    assert broker.positions["AAPL"] == 10


def test_ledger_initializes_sqlite_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ledger = PortfolioLedger(db_path)

    ledger.initialize()

    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert "orders" in tables
