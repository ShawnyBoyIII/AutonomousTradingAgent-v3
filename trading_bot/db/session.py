from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from trading_bot.config import Settings


def _resolve_db_path(settings: Settings) -> Path:
    return Path(settings.app.state_db_path).resolve()


def _make_engine(db_path: Path) -> any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    # We must try to connect once to ensure the file is actually created by SQLAlchemy
    with engine.connect() as conn:
        pass

    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass

    return engine


def init_db(settings: Settings) -> any:
    from trading_bot.db.models import Base
    from sqlalchemy import text

    db_path = _resolve_db_path(settings)
    engine = _make_engine(db_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for ddl in (
            "ALTER TABLE trades ADD COLUMN signal_quality VARCHAR(20)",
            "ALTER TABLE trades ADD COLUMN market_regime VARCHAR(40)",
            "ALTER TABLE trades ADD COLUMN supermodel_decision VARCHAR(20)",
            "ALTER TABLE trades ADD COLUMN consensus VARCHAR(20)",
            "ALTER TABLE trades ADD COLUMN entry_volume_ratio FLOAT",
            "ALTER TABLE trades ADD COLUMN entry_range_ratio FLOAT",
            "ALTER TABLE trades ADD COLUMN adaptive_rr FLOAT",
            "ALTER TABLE trades ADD COLUMN exit_rsi FLOAT",
            "ALTER TABLE trades ADD COLUMN exit_atr FLOAT",
            "ALTER TABLE trades ADD COLUMN hold_duration_minutes FLOAT",
            "ALTER TABLE trades ADD COLUMN exit_regime VARCHAR(40)",
            "ALTER TABLE trades ADD COLUMN exit_strategy VARCHAR(200)",
            "ALTER TABLE trades ADD COLUMN exit_reason VARCHAR(50)",
            "ALTER TABLE trades ADD COLUMN partial_exit_count INTEGER DEFAULT 0",
            "ALTER TABLE trades ADD COLUMN partial_pnl_accumulated FLOAT DEFAULT 0.0",
            "ALTER TABLE positions ADD COLUMN entry_fees FLOAT DEFAULT 0.0",
        ):
            try:
                conn.execute(text(ddl))
            except OperationalError:
                pass  # column already exists
    return engine


def make_session_factory(engine: any) -> sessionmaker:
    return sessionmaker(bind=engine)


def get_session(session_factory: sessionmaker) -> Session:
    return session_factory()
