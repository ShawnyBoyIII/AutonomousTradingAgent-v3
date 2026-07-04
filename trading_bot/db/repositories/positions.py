from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.db.models import Position


def upsert_position(
    session: Session,
    ticker: str,
    quantity: int,
    average_cost: float,
    stop_loss: float | None = None,
    profit_target: float | None = None,
    highest_high: float | None = None,
    strategy_tag: str | None = None,
) -> Position:
    existing = session.execute(
        select(Position).where(
            Position.ticker == ticker,
            Position.closed_at.is_(None),
        )
    ).scalar_one_or_none()

    if existing:
        existing.quantity = quantity
        existing.average_cost = average_cost
        existing.stop_loss = stop_loss
        existing.profit_target = profit_target
        existing.highest_high = highest_high
        if strategy_tag:
            existing.strategy_tag = strategy_tag
    else:
        existing = Position(
            ticker=ticker,
            quantity=quantity,
            average_cost=average_cost,
            stop_loss=stop_loss,
            profit_target=profit_target,
            highest_high=highest_high,
            entry_at=datetime.now(timezone.utc),
            strategy_tag=strategy_tag,
        )
        session.add(existing)

    session.commit()
    session.refresh(existing)
    return existing


def close_position(session: Session, ticker: str) -> Position | None:
    position = session.execute(
        select(Position).where(
            Position.ticker == ticker,
            Position.closed_at.is_(None),
        )
    ).scalar_one_or_none()

    if position:
        position.closed_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(position)
    return position


def get_open_positions(session: Session) -> list[Position]:
    return session.execute(
        select(Position).where(Position.closed_at.is_(None))
    ).scalars().all()
