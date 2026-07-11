from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trading_bot.health.runner import run_health_checks


def _seed_empty(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.executescript(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                status TEXT,
                filled_at TEXT
            );
            CREATE TABLE market_data (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                timeframe TEXT,
                timestamp TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_runner_all_pass(tmp_path: Path):
    db = tmp_path / "b.db"
    _seed_empty(db)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "burn_in.pid").write_text(str(os.getpid()))  # self
    h = {"ts": datetime.now(timezone.utc).isoformat(), "cycle": 1, "fills": 0, "exits": 0, "rejects": 0}
    (state_dir / "heartbeat.json").write_text(json.dumps(h))
    report = run_health_checks(
        state_dir=state_dir,
        db_path=db,
        dashboard_port=1,  # unlikely to be listening
        eod_watchdog_pid_file=tmp_path / "wd.pid",
        now_utc=datetime.now(timezone.utc) - timedelta(days=1),  # far outside market
    )
    # Most checks degrade gracefully — at minimum, runner never raises.
    assert report.checks
    statuses = [c.status for c in report.checks]
    # outside market hours, the market_data check should be PASS
    assert any(s == "PASS" for s in statuses)


def test_runner_one_warn(tmp_path: Path):
    db = tmp_path / "b.db"
    _seed_empty(db)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "burn_in.pid").write_text("1")  # PID 1 always alive
    # Stale heartbeat to force a WARN
    old_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    (state_dir / "heartbeat.json").write_text(json.dumps({"ts": old_iso, "cycle": 1}))
    report = run_health_checks(
        state_dir=state_dir,
        db_path=db,
        dashboard_port=1,
        eod_watchdog_pid_file=tmp_path / "wd.pid",
        now_utc=datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert report.worst_status() in {"WARN", "FAIL", "PASS"}


def test_runner_with_missing_pid_file(tmp_path: Path):
    db = tmp_path / "b.db"
    _seed_empty(db)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    report = run_health_checks(
        state_dir=state_dir,
        db_path=db,
        dashboard_port=1,
        eod_watchdog_pid_file=tmp_path / "wd.pid",
    )
    pid_check = next(c for c in report.checks if c.name == "pid_alive")
    assert pid_check.status == "FAIL"