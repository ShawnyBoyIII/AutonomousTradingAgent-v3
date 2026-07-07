from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.db.models import ScanFeature


def upsert_scan_feature(
    session: Session,
    *,
    scan_result_id: int | None = None,
    ticker: str,
    status: str,
    action: str,
    confidence: float | None = None,
    quality: str | None = None,
    freshness: str | None = None,
    market_age_minutes: int | None = None,
    market_regime: str | None = None,
    strategy_tag: str | None = None,
    consensus: str | None = None,
    v3_total_score: float | None = None,
    supermodel_score: float | None = None,
    mtf_aligned: int | None = None,
    entry_volume_ratio: float | None = None,
    entry_range_ratio: float | None = None,
    adaptive_rr: float | None = None,
) -> ScanFeature:
    feature = ScanFeature(
        scan_result_id=scan_result_id,
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        status=status,
        action=action,
        confidence=confidence,
        quality=quality,
        freshness=freshness,
        market_age_minutes=market_age_minutes,
        market_regime=market_regime,
        strategy_tag=strategy_tag,
        consensus=consensus,
        v3_total_score=v3_total_score,
        supermodel_score=supermodel_score,
        mtf_aligned=mtf_aligned,
        entry_volume_ratio=entry_volume_ratio,
        entry_range_ratio=entry_range_ratio,
        adaptive_rr=adaptive_rr,
    )
    session.add(feature)
    session.commit()
    session.refresh(feature)
    return feature


def get_scan_features(
    session: Session,
    ticker: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    status: str | None = None,
    action: str | None = None,
    market_regime: str | None = None,
    quality: str | None = None,
    strategy_tag: str | None = None,
) -> list[ScanFeature]:
    query = select(ScanFeature)
    if ticker:
        query = query.where(ScanFeature.ticker == ticker)
    if since:
        query = query.where(ScanFeature.timestamp >= since)
    if status:
        query = query.where(ScanFeature.status == status)
    if action:
        query = query.where(ScanFeature.action == action)
    if market_regime:
        query = query.where(ScanFeature.market_regime == market_regime)
    if quality:
        query = query.where(ScanFeature.quality == quality)
    if strategy_tag:
        query = query.where(ScanFeature.strategy_tag == strategy_tag)
    query = query.order_by(ScanFeature.timestamp.desc())
    if limit:
        query = query.limit(limit)
    return session.execute(query).scalars().all()
