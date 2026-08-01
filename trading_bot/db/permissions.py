from __future__ import annotations

import os
from pathlib import Path


SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def secure_sqlite_artifacts(db_path: str | Path) -> None:
    """Restrict a SQLite database and any existing journal artifacts."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")

    artifacts = (
        path,
        *(path.with_name(path.name + suffix) for suffix in SQLITE_SIDECAR_SUFFIXES),
    )
    for artifact in artifacts:
        if not artifact.exists():
            continue
        try:
            os.chmod(artifact, 0o600)
        except OSError as exc:
            raise PermissionError(
                f"Unable to secure SQLite artifact permissions: {artifact}"
            ) from exc
