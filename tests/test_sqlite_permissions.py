from __future__ import annotations

import os
from pathlib import Path

import pytest

from trading_bot.db.permissions import secure_sqlite_artifacts


def test_secure_sqlite_artifacts_covers_existing_sidecars(tmp_path: Path) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()
    sidecars = [
        tmp_path / f"portfolio.db{suffix}"
        for suffix in ("-wal", "-shm", "-journal")
    ]
    for artifact in sidecars:
        artifact.touch()
        os.chmod(artifact, 0o644)
    os.chmod(db_path, 0o644)

    secure_sqlite_artifacts(db_path)

    assert (db_path.stat().st_mode & 0o777) == 0o600
    for artifact in sidecars:
        assert (artifact.stat().st_mode & 0o777) == 0o600


def test_secure_sqlite_artifacts_surfaces_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "portfolio.db"
    db_path.touch()

    def deny_chmod(_path: Path, _mode: int) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("trading_bot.db.permissions.os.chmod", deny_chmod)

    with pytest.raises(PermissionError, match="Unable to secure SQLite artifact"):
        secure_sqlite_artifacts(db_path)


def test_sqlite_writers_secure_their_database_files(tmp_path: Path) -> None:
    from trading_bot.data.cache import MarketDataCache
    from trading_bot.data.data_store import DataStoreManifest
    from trading_bot.memory.store import MemoryStore
    from trading_bot.portfolio.ledger import PortfolioLedger
    from trading_bot.research.store import ResearchStore

    paths = [
        tmp_path / "cache.db",
        tmp_path / "data-store.db",
        tmp_path / "memory.db",
        tmp_path / "portfolio.db",
        tmp_path / "research.db",
    ]
    MarketDataCache(paths[0])
    DataStoreManifest(paths[1])
    MemoryStore(str(paths[2]))
    PortfolioLedger(paths[3]).initialize()
    ResearchStore(str(paths[4]))

    for path in paths:
        assert (path.stat().st_mode & 0o777) == 0o600
