from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from trading_bot.db.models import Event


def log_event(
    session: Session,
    event_type: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    details: str | dict | None = None,
) -> Event:
    if isinstance(details, dict):
        import json

        details = json.dumps(details)
    event = Event(
        timestamp=datetime.now(timezone.utc),
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )
    session.add(event)
    session.commit()
    session.refresh(event)
    return event


def get_events(
    session: Session,
    event_type: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[Event]:
    query = select(Event).order_by(desc(Event.timestamp))
    if event_type:
        query = query.where(Event.event_type == event_type)
    if since:
        query = query.where(Event.timestamp >= since)
    if limit:
        query = query.limit(limit)
    return session.execute(query).scalars().all()
