from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from trading_bot.learning.experiments.models import ExperimentState


class ExperimentStore:
    """Atomic, append-only storage for tuning experiments."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def artifact_dir(self) -> Path:
        return self.root  # caller may use subdirs per experiment-id

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def load_current(self) -> ExperimentState | None:
        if not self.current_path.exists():
            return None
        payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        return ExperimentState.model_validate(payload)

    def save_current(self, state: ExperimentState) -> None:
        self._ensure_root()
        payload = state.model_dump(mode="json")
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.root, delete=False
        ) as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            temp_path = Path(handle.name)
        temp_path.replace(self.current_path)

    def append_event(self, event: dict[str, Any]) -> None:
        self._ensure_root()
        payload = dict(event)
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(payload, sort_keys=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def snapshot_overrides(
        self, experiment_id: str, name: str, overrides: dict[str, object]
    ) -> Path:
        self._ensure_root()
        target_dir = self.root / experiment_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.yaml"
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir, delete=False
        ) as handle:
            yaml.safe_dump(overrides, handle, sort_keys=False)
            temp_path = Path(handle.name)
        temp_path.replace(path)
        return path

    def restore_baseline(
        self, experiment_id: str, target_path: Path
    ) -> bool:
        snapshot = self.root / experiment_id / "baseline.yaml"
        if not snapshot.exists():
            return False
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_path.parent, delete=False
        ) as handle:
            handle.write(snapshot.read_text(encoding="utf-8"))
            temp_path = Path(handle.name)
        temp_path.replace(target_path)
        return True

    def clear_current(self) -> None:
        if self.current_path.exists():
            self.current_path.unlink()

    def write_overrides_atomic(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            yaml.safe_dump(payload, handle, sort_keys=False)
            temp_path = Path(handle.name)
        temp_path.replace(path)