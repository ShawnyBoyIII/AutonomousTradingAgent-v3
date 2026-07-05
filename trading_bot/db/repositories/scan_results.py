from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.db.models import ScanResult


def upsert_scan_result(
    session: Session,
    ticker: str,
    action: str,
    confidence: float,
    score: float | None = None,
    strategy_tag: str | None = None,
    reasons: list[str] | None = None,
    details: dict | None = None,
) -> ScanResult:
    result = ScanResult(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        action=action,
        confidence=confidence,
        score=score,
        strategy_tag=strategy_tag,
        reasons=json.dumps(reasons) if reasons else None,
        details=json.dumps(details) if details else None,
    )
    session.add(result)
    session.commit()
    session.refresh(result)
    return result


def get_scan_results(
    session: Session,
    ticker: str | None = None,
    since: datetime | None = None,
    action: str | None = None,
    limit: int | None = None,
) -> list[ScanResult]:
    query = select(ScanResult)
    if ticker:
        query = query.where(ScanResult.ticker == ticker)
    if since:
        query = query.where(ScanResult.timestamp >= since)
    if action:
        query = query.where(ScanResult.action == action)
    query = query.order_by(ScanResult.timestamp.desc())
    if limit:
        query = query.limit(limit)
    return session.execute(query).scalars().all()
