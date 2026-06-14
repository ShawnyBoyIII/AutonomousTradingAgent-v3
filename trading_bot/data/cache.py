from __future__ import annotations

from pathlib import Path


def ensure_cache_dir(path: str) -> Path:
    cache_dir = Path(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
