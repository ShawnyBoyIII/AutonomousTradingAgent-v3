from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MarketData(Base):
    __tablename__ = "market_data"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    timeframe = Column(String(20), nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    fetched_at = Column(DateTime, nullable=False, default=utc_now)

    __table_args__ = (
        UniqueConstraint("ticker", "timeframe", "timestamp", name="uq_market_data"),
    )


class ScanResult(Base):
    __tablename__ = "scan_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=utc_now, index=True)
    action = Column(String(10), nullable=False)
    confidence = Column(Float, nullable=False)
    score = Column(Float, nullable=True)
    strategy_tag = Column(String(50), nullable=True)
    reasons = Column(Text, nullable=True)
    details = Column(Text, nullable=True)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    order_type = Column(String(20), nullable=False)
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    profit_target = Column(Float, nullable=True)
    fees = Column(Float, nullable=False, default=0.0)
    filled_at = Column(DateTime, nullable=False, default=utc_now, index=True)
    strategy_tag = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, default="FILLED")
    exit_price = Column(Float, nullable=True)
    exit_fees = Column(Float, nullable=False, default=0.0)
    exited_at = Column(DateTime, nullable=True)
    pnl = Column(Float, nullable=True)


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    quantity = Column(Integer, nullable=False)
    average_cost = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=True)
    profit_target = Column(Float, nullable=True)
    highest_high = Column(Float, nullable=True)
    entry_at = Column(DateTime, nullable=True)
    strategy_tag = Column(String(50), nullable=True)
    closed_at = Column(DateTime, nullable=True)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=utc_now, index=True)
    cash = Column(Float, nullable=False)
    equity = Column(Float, nullable=False)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    num_positions = Column(Integer, nullable=False, default=0)


class ModelPrediction(Base):
    __tablename__ = "model_predictions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, default=utc_now, index=True)
    action = Column(Integer, nullable=False)
    confidence = Column(Float, nullable=False)
    model_path = Column(String(255), nullable=True)
    observation_hash = Column(String(64), nullable=True)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=utc_now, index=True)
    event_type = Column(String(50), nullable=False)
    entity_type = Column(String(50), nullable=True)
    entity_id = Column(Integer, nullable=True)
    details = Column(Text, nullable=True)
