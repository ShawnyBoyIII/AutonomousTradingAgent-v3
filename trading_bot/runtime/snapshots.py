from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.reports.exporters import export_json


def write_snapshot(path: str | Path, payload: dict[str, Any]) -> None:
    snapshot = {
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        **payload,
    }
    export_json(snapshot, Path(path))


def read_recent_decision_rows(path: str | Path, limit: int = 10) -> list[dict[str, Any]]:
    log_path = Path(path)
    if not log_path.exists():
        return []

    rows: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            import json

            rows.append(json.loads(line))
        except Exception:
            continue
    return rows[-limit:]
