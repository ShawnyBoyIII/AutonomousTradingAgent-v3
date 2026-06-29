from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.db.models import Trade


def upsert_trade(
    session: Session,
    ticker: str,
    side: str,
    order_type: str,
    quantity: int,
    entry_price: float,
    stop_loss: float | None = None,
    profit_target: float | None = None,
    fees: float = 0.0,
    strategy_tag: str | None = None,
    status: str = "FILLED",
) -> Trade:
    trade = Trade(
        ticker=ticker,
        side=side,
        order_type=order_type,
        quantity=quantity,
        entry_price=entry_price,
        stop_loss=stop_loss,
        profit_target=profit_target,
        fees=fees,
        filled_at=datetime.now(timezone.utc),
        strategy_tag=strategy_tag,
        status=status,
    )
    session.add(trade)
    session.commit()
    session.refresh(trade)
    return trade


def update_trade_exit(
    session: Session,
    trade_id: int,
    exit_price: float,
    exit_fees: float = 0.0,
    pnl: float | None = None,
) -> Trade:
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise ValueError(f"Trade {trade_id} not found")
    trade.exit_price = exit_price
    trade.exit_fees = exit_fees
    trade.exited_at = datetime.utcnow()
    trade.pnl = pnl
    trade.status = "CLOSED"
    session.commit()
    session.refresh(trade)
    return trade


def get_open_trades(session: Session) -> list[Trade]:
    return session.execute(
        select(Trade).where(Trade.status == "FILLED")
    ).scalars().all()


def get_trades(
    session: Session,
    ticker: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[Trade]:
    query = select(Trade)
    if ticker:
        query = query.where(Trade.ticker == ticker)
    if since:
        query = query.where(Trade.filled_at >= since)
    query = query.order_by(Trade.filled_at.desc())
    if limit:
        query = query.limit(limit)
    return session.execute(query).scalars().all()
