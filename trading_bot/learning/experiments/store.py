from __future__ import annotations

import hashlib
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

    def snapshot_overrides_bytes(
        self, experiment_id: str, name: str, raw_bytes: bytes
    ) -> Path:
        """Record the original bytes of an override file verbatim.

        Used by ``propose`` so rollback can restore the exact operator-authored
        contents (including comments, key order, and quoting) rather than a
        YAML re-dump of the parsed structure.
        """
        self._ensure_root()
        target_dir = self.root / experiment_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.yaml"
        with NamedTemporaryFile("wb", dir=target_dir, delete=False) as handle:
            handle.write(raw_bytes)
            temp_path = Path(handle.name)
        temp_path.replace(path)
        return path

    def snapshot_absent_baseline(self, experiment_id: str) -> Path:
        """Mark the baseline as 'the overrides file did not exist at
        propose time' so rollback can leave it absent."""
        self._ensure_root()
        target_dir = self.root / experiment_id
        target_dir.mkdir(parents=True, exist_ok=True)
        marker = target_dir / "baseline.absent"
        marker.write_text("", encoding="utf-8")
        return marker

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

    @staticmethod
    def checksum(path: Path) -> str:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def write_overrides_bytes_atomic(self, target: Path, raw_bytes: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
            handle.write(raw_bytes)
            temp_path = Path(handle.name)
        temp_path.replace(target)

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

    def restore_baseline_exact(
        self, experiment_id: str, target: Path
    ) -> bool:
        """Restore baseline verbatim: exact bytes if present, deletion if
        the original file was absent, no-op otherwise.

        Returns True when the operation succeeded, False when no snapshot
        exists for this experiment id.
        """
        absent_marker = self.root / experiment_id / "baseline.absent"
        snapshot = self.root / experiment_id / "baseline.yaml"
        if absent_marker.exists():
            if target.exists():
                target.unlink()
            return True
        if not snapshot.exists():
            return False
        self.write_overrides_bytes_atomic(target, snapshot.read_bytes())
        return True

    def activate_candidate(self, experiment_id: str, target: Path) -> bool:
        """Write the candidate snapshot bytes to the active overrides path.

        Used by ``evaluate`` when an experiment transitions to CANARY.
        Returns False if the candidate snapshot is missing.
        """
        snapshot = self.root / experiment_id / "candidate.yaml"
        if not snapshot.exists():
            return False
        self.write_overrides_bytes_atomic(target, snapshot.read_bytes())
        return True

    def detect_candidate_drift(
        self, experiment_id: str, expected_checksum: str | None
    ) -> bool:
        """True when the live candidate snapshot has been mutated away
        from the bytes recorded at proposal time."""
        snapshot = self.root / experiment_id / "candidate.yaml"
        if not snapshot.exists():
            return True
        if expected_checksum is None:
            return False
        return self.checksum(snapshot) != expected_checksum

    def detect_baseline_drift(
        self, target: Path, expected_checksum: str | None, baseline_was_absent: bool
    ) -> bool:
        """True when the live overrides file has been mutated away from
        the bytes recorded at proposal time.

        ``expected_checksum`` is the baseline checksum recorded when the
        experiment was proposed; ``baseline_was_absent`` flags whether the
        baseline file did not exist at proposal time. This guards against
        the controller silently overwriting an operator's hand-edits at
        activation or rollback.
        """
        if baseline_was_absent:
            # If baseline was absent at proposal time, the only "drift" we
            # worry about is the operator creating the file from scratch.
            return target.exists()
        if not target.exists():
            return True  # original baseline existed but is now missing
        if expected_checksum is None:
            return False  # nothing to compare against; treat as no drift
        return self.checksum(target) != expected_checksum

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