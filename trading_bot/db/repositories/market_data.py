from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from trading_bot.db.models import MarketData


def upsert_market_bars(session: Session, ticker: str, timeframe: str, bars: pd.DataFrame) -> int:
    if bars.empty:
        return 0

    # Pre-process timestamps
    processed_bars = []
    timestamps = []
    for row in bars.to_dict('records'):
        ts = row["timestamp"]
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        row["timestamp_obj"] = ts
        timestamps.append(ts)
        processed_bars.append(row)

    # In SQLite timezone-aware datetimes might be stored as naive UTC
    # To reliably match, we should do our mapped lookup ensuring we
    # compare them the same way they come out of the DB.

    # Bulk fetch existing market data to avoid N+1 queries
    existing_records = session.execute(
        select(MarketData).where(
            and_(
                MarketData.ticker == ticker,
                MarketData.timeframe == timeframe,
                MarketData.timestamp.in_(timestamps),
            )
        )
    ).scalars().all()

    existing_map = {}
    for record in existing_records:
        ts = record.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        existing_map[ts] = record

    count = 0
    now = datetime.now(timezone.utc)
    for row in processed_bars:
        ts = row["timestamp_obj"]

        # Ensure our lookup key is also tz-aware
        if ts.tzinfo is None:
             ts = ts.replace(tzinfo=timezone.utc)

        existing = existing_map.get(ts)

        if existing:
            existing.open = float(row["open"])
            existing.high = float(row["high"])
            existing.low = float(row["low"])
            existing.close = float(row["close"])
            existing.volume = int(row["volume"])
        else:
            session.add(
                MarketData(
                    ticker=ticker,
                    timeframe=timeframe,
                    timestamp=ts,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row["volume"]),
                    fetched_at=now,
                )
            )
        count += 1

    session.commit()
    return count


def get_market_bars(
    session: Session,
    ticker: str,
    timeframe: str,
    since: datetime | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    query = select(MarketData).where(
        and_(
            MarketData.ticker == ticker,
            MarketData.timeframe == timeframe,
        )
    )
    if since:
        query = query.where(MarketData.timestamp >= since)
    query = query.order_by(MarketData.timestamp)
    if limit:
        query = query.limit(limit)

    rows = session.execute(query).scalars().all()
    if not rows:
        return pd.DataFrame()

    data = [
        {
            "timestamp": r.timestamp,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        }
        for r in rows
    ]
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def get_latest_bar_timestamp(session: Session, ticker: str, timeframe: str) -> datetime | None:
    result = session.execute(
        select(MarketData.timestamp)
        .where(
            and_(
                MarketData.ticker == ticker,
                MarketData.timeframe == timeframe,
            )
        )
        .order_by(MarketData.timestamp.desc())
        .limit(1)
    ).scalar_one_or_none()
    return result


def is_market_data_stale(
    session: Session,
    ticker: str,
    timeframe: str,
    max_age_minutes: int = 30,
) -> bool:
    latest = get_latest_bar_timestamp(session, ticker, timeframe)
    if latest is None:
        return True
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest < cutoff
