from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from trading_bot.db.models import MarketData


def upsert_market_bars(session: Session, ticker: str, timeframe: str, bars: pd.DataFrame) -> int:
    count = 0
    # Optimization: use to_dict('records') which is much faster than iterrows
    for row in bars.to_dict('records'):
        ts = row["timestamp"]
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        existing = session.execute(
            select(MarketData).where(
                and_(
                    MarketData.ticker == ticker,
                    MarketData.timeframe == timeframe,
                    MarketData.timestamp == ts,
                )
            )
        ).scalar_one_or_none()

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
                    fetched_at=datetime.now(timezone.utc),
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
