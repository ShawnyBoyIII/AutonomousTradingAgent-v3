from __future__ import annotations

import json
from datetime import date
from pathlib import Path


def append_decision_event(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, default=str) + "\n")


def should_append_backtest_entry(path: Path, ticker: str) -> bool:
    today = date.today().isoformat()
    if not path.exists():
        return True
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("command") != "backtest":
                    continue
                if entry.get("ticker") != ticker:
                    continue
                entry_date = entry.get("date", entry.get("timestamp", "")).split("T")[0] if "T" in str(entry.get("date", "")) else str(entry.get("date", entry.get("timestamp", "")))[:10]
                if entry_date == today:
                    return False
    except (OSError, IOError):
        pass
    return True
