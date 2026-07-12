from __future__ import annotations

from datetime import datetime, timezone

from trading_bot.execution.paper_broker import PaperBroker
from trading_bot.models.order import OrderRequest


def test_paper_fill_timestamp_is_aware_utc() -> None:
    broker = PaperBroker(starting_cash=10_000.0, fee_per_order=0.0, slippage_bps=0)
    order = OrderRequest(
        ticker="AAPL",
        side="BUY",
        order_type="market",
        quantity=1,
        submitted_at=datetime(2026, 7, 11, 9, 30, tzinfo=timezone.utc),
    )

    fill = broker.submit_order(order, market_price=100.0)

    assert fill.filled_at.tzinfo is not None
    assert fill.filled_at.utcoffset() == timezone.utc.utcoffset(fill.filled_at)
