from __future__ import annotations

import json
from pathlib import Path


def rl_model_meta_path(model_path: Path) -> Path:
    path = model_path.with_suffix(".zip")
    if path.suffix != ".zip":
        path = model_path
    return path.parent / f"{path.stem}_meta.json"


def rl_model_symbols(model_path: Path) -> list[str] | None:
    meta_path = rl_model_meta_path(model_path)
    if not meta_path.exists():
        return None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return [str(value).upper().strip() for value in meta.get("symbols", [])]
