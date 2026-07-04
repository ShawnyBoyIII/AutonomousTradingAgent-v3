from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from trading_bot.config import Settings


def _resolve_db_path(settings: Settings) -> Path:
    return (Path(settings.app.log_dir).parent / "state" / "trading_bot.db").resolve()


def _make_engine(db_path: Path) -> any:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def init_db(settings: Settings) -> any:
    from trading_bot.db.models import Base

    db_path = _resolve_db_path(settings)
    engine = _make_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: any) -> sessionmaker:
    return sessionmaker(bind=engine)


def get_session(session_factory: sessionmaker) -> Session:
    return session_factory()
