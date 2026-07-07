from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from trading_bot.config import Settings


def _resolve_db_path(settings: Settings) -> Path:
    return Path(settings.app.state_db_path).resolve()


def _make_engine(db_path: Path) -> any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


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
        ):
            try:
                conn.execute(text(ddl))
            except Exception:
                logger = __import__("logging").getLogger(__name__)
                logger.warning("DDL skipped (column may already exist): %s", ddl)
    return engine


def make_session_factory(engine: any) -> sessionmaker:
    return sessionmaker(bind=engine)


def get_session(session_factory: sessionmaker) -> Session:
    return session_factory()
