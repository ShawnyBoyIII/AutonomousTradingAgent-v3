from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading_bot.db.models import ModelPrediction


def upsert_prediction(
    session: Session,
    ticker: str,
    action: int,
    confidence: float,
    model_path: str | None = None,
    observation: list[float] | None = None,
) -> ModelPrediction:
    obs_hash = None
    if observation:
        obs_bytes = str(observation).encode("utf-8")
        obs_hash = hashlib.sha256(obs_bytes).hexdigest()

    pred = ModelPrediction(
        ticker=ticker,
        timestamp=datetime.now(timezone.utc),
        action=action,
        confidence=confidence,
        model_path=model_path,
        observation_hash=obs_hash,
    )
    session.add(pred)
    session.commit()
    session.refresh(pred)
    return pred


def get_predictions(
    session: Session,
    ticker: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[ModelPrediction]:
    query = select(ModelPrediction)
    if ticker:
        query = query.where(ModelPrediction.ticker == ticker)
    if since:
        query = query.where(ModelPrediction.timestamp >= since)
    query = query.order_by(ModelPrediction.timestamp.desc())
    if limit:
        query = query.limit(limit)
    return session.execute(query).scalars().all()
