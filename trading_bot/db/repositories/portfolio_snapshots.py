from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.db.models import PortfolioSnapshot


def create_snapshot(
    session: Session,
    cash: float,
    equity: float,
    unrealized_pnl: float = 0.0,
    realized_pnl: float = 0.0,
    num_positions: int = 0,
) -> PortfolioSnapshot:
    snapshot = PortfolioSnapshot(
        timestamp=datetime.now(timezone.utc),
        cash=cash,
        equity=equity,
        unrealized_pnl=unrealized_pnl,
        realized_pnl=realized_pnl,
        num_positions=num_positions,
    )
    session.add(snapshot)
    session.commit()
    session.refresh(snapshot)
    return snapshot


def get_snapshots(
    session: Session,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[PortfolioSnapshot]:
    query = select(PortfolioSnapshot)
    if since:
        query = query.where(PortfolioSnapshot.timestamp >= since)
    query = query.order_by(PortfolioSnapshot.timestamp.desc())
    if limit:
        query = query.limit(limit)
    return session.execute(query).scalars().all()
