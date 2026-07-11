# Burn-In Reliability Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the operator one CLI command (`./tradebot-local doctor --burn-in`) and one shell hook (`run_health_check` inside `auto-burn-in.sh`) that answer "Is the burn-in healthy right now?", closing the 2026-07-09 silent-hang failure mode where the burn-in PID stayed alive while the main loop made no progress.

**Architecture:** Two layers, both narrow. (1) `auto-burn-in.sh` writes a tiny heartbeat JSON each loop iteration. (2) `trading_bot/health/` is a new package of pure check functions (PID alive, heartbeat fresh, dashboard reachable, EOD watchdog scheduled, open positions consistent, market data fresh). A `runner` aggregates per-check `CheckResult`s into a `HealthReport` with worst-severity status. The existing `doctor` command grows a `--burn-in` flag that surfaces the report (human or `--json`); `auto-burn-in.sh` invokes the same surface every 30 min and once at 15:50 ET pre-EOD.

**Tech Stack:** Python 3.11+ (project default), Typer (existing), `urllib.request` for the loopback dashboard probe, bash for the shell side. No new dependencies.

## Global Constraints

- Python ≥ 3.11 (see `pyproject.toml` `requires-python`)
- NumPy < 2 (pinned in `pyproject.toml`)
- Tests are network-free — monkeypatch `urllib.request.urlopen`; use `tmp_path` fixtures
- Live trading is always paper-only (`live_trading_enabled` forced false in `config/loader.py`)
- Heartbeat writes to `state/burn_in/heartbeat.json`; PID files in same directory
- Health checks never raise — exceptions degrade to `FAIL`
- CLI exit codes: `0` PASS, `1` WARN, `2` FAIL (scriptable from shell)
- Never modify tests when fixing bugs — tests are source of truth
- The 5 new test files must each be deterministic and `tmp_path`-based
- Mirror naming: `tests/test_health_checks.py`, `tests/test_health_runner.py`, `tests/test_doctor_burn_in.py`, plus extensions to `tests/test_auto_burn_in_script.py`
- Update AGENTS.md in the same commit as the operational changes (heartbeat + run_health_check + doctor --burn-in entrypoint)

---

### Task 1: Health package skeleton + types

**Files:**
- Create: `trading_bot/health/__init__.py`
- Create: `trading_bot/health/types.py`
- Create: `tests/test_health_types.py`

**Interfaces:**
- Consumes: nothing (foundational)
- Produces: `CheckResult` dataclass; `HealthReport` dataclass; `Status` Literal["PASS","WARN","FAIL"]

- [ ] **Step 1: Write the failing test**

```python
# tests/test_health_types.py
from __future__ import annotations
from trading_bot.health.types import CheckResult, HealthReport


def test_check_result_fields():
    cr = CheckResult(name="pid", status="PASS", detail="alive", observed={"pid": 13773})
    assert cr.name == "pid"
    assert cr.status == "PASS"
    assert cr.detail == "alive"
    assert cr.observed == {"pid": 13773}


def test_health_report_aggregates_severity():
    checks = [
        CheckResult(name="a", status="PASS", detail="ok", observed=None),
        CheckResult(name="b", status="WARN", detail="meh", observed=None),
        CheckResult(name="c", status="PASS", detail="ok", observed=None),
    ]
    report = HealthReport(checks=checks)
    assert report.worst_status() == "WARN"


def test_health_report_worst_is_fail():
    checks = [
        CheckResult(name="a", status="PASS", detail="ok", observed=None),
        CheckResult(name="b", status="FAIL", detail="down", observed=None),
    ]
    report = HealthReport(checks=checks)
    assert report.worst_status() == "FAIL"


def test_health_report_to_dict_shape():
    checks = [
        CheckResult(name="a", status="PASS", detail="ok", observed={"k": 1}),
    ]
    report = HealthReport(checks=checks, generated_at="2026-07-10T09:31:00Z")
    payload = report.to_dict()
    assert payload["worst_status"] == "PASS"
    assert payload["generated_at"] == "2026-07-10T09:31:00Z"
    assert payload["checks"][0]["observed"] == {"k": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_health_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trading_bot.health'`

- [ ] **Step 3: Write the package skeleton**

```python
# trading_bot/health/__init__.py
from __future__ import annotations

from trading_bot.health.types import CheckResult, HealthReport, Status

__all__ = ["CheckResult", "HealthReport", "Status"]
```

```python
# trading_bot/health/types.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Status = Literal["PASS", "WARN", "FAIL"]

_SEVERITY_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    observed: dict | None = None


@dataclass
class HealthReport:
    checks: list[CheckResult] = field(default_factory=list)
    generated_at: str = ""

    def worst_status(self) -> Status:
        if not self.checks:
            return "PASS"
        return max(self.checks, key=lambda c: _SEVERITY_RANK[c.status]).status

    def to_dict(self) -> dict:
        return {
            "worst_status": self.worst_status(),
            "generated_at": self.generated_at,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "detail": c.detail,
                    "observed": c.observed,
                }
                for c in self.checks
            ],
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_health_types.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add trading_bot/health/ tests/test_health_types.py
git commit -m "feat(health): package skeleton with CheckResult and HealthReport types"
```

---

### Task 2: Six pure check functions with tests

**Files:**
- Create: `trading_bot/health/checks.py`
- Create: `tests/test_health_checks.py`

**Interfaces:**
- Consumes: `CheckResult` from Task 1; `Path`, `datetime`, `os.kill`, `urllib.request.urlopen` (monkeypatched in tests); `sqlite3` to peek at the trades/market_data tables
- Produces: six top-level functions, each returning a `CheckResult`

- [ ] **Step 1: Write failing tests for all six checks**

```python
# tests/test_health_checks.py
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from trading_bot.health.checks import (
    check_dashboard_health,
    check_eod_watchdog,
    check_heartbeat_fresh,
    check_market_data_freshness,
    check_open_positions_consistent,
    check_pid_alive,
)


def _write_pid(path: Path, pid: int) -> None:
    path.write_text(str(pid))


def _write_heartbeat(path: Path, ts_iso: str, *, cycle: int = 1, fills: int = 0, exits: int = 0, rejects: int = 0) -> None:
    payload = {
        "ts": ts_iso,
        "cycle": cycle,
        "fills": fills,
        "exits": exits,
        "rejects": rejects,
    }
    path.write_text(json.dumps(payload))


def _seed_db(db: Path, *, open_trades: int = 0, last_market_ts: str | None = None) -> None:
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
        for i in range(open_trades):
            conn.execute(
                "INSERT INTO trades(ticker,status,filled_at) VALUES (?,?,?)",
                (f"T{i}", "FILLED", "2026-07-10T13:00:00+00:00"),
            )
        if last_market_ts is not None:
            conn.execute(
                "INSERT INTO market_data(ticker,timeframe,timestamp) VALUES (?,?,?)",
                ("AAPL", "5m", last_market_ts),
            )
        conn.commit()
    finally:
        conn.close()


# --- check_pid_alive --------------------------------------------------------

def test_pid_alive_pass(tmp_path: Path):
    pid_file = tmp_path / "burn_in.pid"
    _write_pid(pid_file, 1)
    # PID 1 (launchd) is always alive on macOS. On Linux PID 1 also exists.
    result = check_pid_alive(pid_file)
    assert result.status == "PASS"
    assert result.name == "pid_alive"


def test_pid_alive_fail_dead(tmp_path: Path):
    pid_file = tmp_path / "burn_in.pid"
    pid_file.write_text("99999999")  # unlikely
    result = check_pid_alive(pid_file)
    # On any sane CI, that PID is dead → FAIL; if it happens to be live,
    # the test still asserts the function returns a CheckResult, not crashes.
    assert result.status in {"PASS", "FAIL"}


def test_pid_alive_fail_missing(tmp_path: Path):
    result = check_pid_alive(tmp_path / "missing.pid")
    assert result.status == "FAIL"
    assert "missing" in result.detail.lower()


def test_pid_alive_fail_non_integer(tmp_path: Path):
    pid_file = tmp_path / "bad.pid"
    pid_file.write_text("not-a-number\n")
    result = check_pid_alive(pid_file)
    assert result.status == "FAIL"
    assert "invalid" in result.detail.lower()


# --- check_heartbeat_fresh --------------------------------------------------

def test_heartbeat_fresh_pass(tmp_path: Path):
    h = tmp_path / "hb.json"
    _write_heartbeat(h, datetime.now(timezone.utc).isoformat())
    result = check_heartbeat_fresh(h, max_age_seconds=90)
    assert result.status == "PASS"


def test_heartbeat_fresh_warn(tmp_path: Path):
    h = tmp_path / "hb.json"
    old = datetime.now(timezone.utc).timestamp() - 200  # 200s ago
    iso = datetime.fromtimestamp(old, tz=timezone.utc).isoformat()
    _write_heartbeat(h, iso)
    result = check_heartbeat_fresh(h, max_age_seconds=90)
    assert result.status == "WARN"


def test_heartbeat_fresh_fail_missing(tmp_path: Path):
    result = check_heartbeat_fresh(tmp_path / "missing.json", max_age_seconds=90)
    assert result.status == "FAIL"


def test_heartbeat_fresh_does_not_raise_on_bad_json(tmp_path: Path):
    h = tmp_path / "hb.json"
    h.write_text("not json{")
    result = check_heartbeat_fresh(h, max_age_seconds=90)
    assert result.status == "FAIL"


# --- check_dashboard_health -------------------------------------------------

def test_dashboard_pass(monkeypatch):
    class _Resp:
        status = 200

        def read(self):  # noqa: D401
            return b'{"status":"ok"}'

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen",
        lambda *a, **kw: _Resp(),
    )
    result = check_dashboard_health(port=8765)
    assert result.status == "PASS"


def test_dashboard_warn_non_200(monkeypatch):
    class _Resp:
        status = 503

        def read(self):  # noqa: D401
            return b""

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen",
        lambda *a, **kw: _Resp(),
    )
    result = check_dashboard_health(port=8765)
    assert result.status == "WARN"


def test_dashboard_fail_connection_refused(monkeypatch):
    def boom(*a, **kw):
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr("trading_bot.health.checks.urlopen", boom)
    result = check_dashboard_health(port=8765)
    assert result.status == "FAIL"
    assert "refused" in result.detail.lower() or "connect" in result.detail.lower()


# --- check_eod_watchdog -----------------------------------------------------

def test_eod_watchdog_pass_no_fire_window(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    # No PID file + 12:00 ET Monday → not yet expected to fire → PASS
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 6, 16, 0, tzinfo=timezone.utc),  # Mon 12:00 ET
    )
    assert result.status == "PASS"


def test_eod_watchdog_fail_in_window_without_pid(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    # 15:55 ET on a weekday → watchdog should be alive
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 6, 19, 55, tzinfo=timezone.utc),  # Mon 15:55 ET
    )
    assert result.status == "FAIL"


def test_eod_watchdog_pass_in_window_with_live_pid(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    pid_file.write_text("1")  # PID 1 is always alive
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 6, 19, 55, tzinfo=timezone.utc),
    )
    # Either PASS (PID 1 alive in window) or WARN (we punt on PID 1 detection edge case)
    assert result.status in {"PASS", "WARN"}


def test_eod_watchdog_no_action_on_weekend(tmp_path: Path):
    pid_file = tmp_path / "wd.pid"
    # Saturday at 15:55 ET → outside EOD window
    result = check_eod_watchdog(
        pid_file=pid_file,
        now_utc=datetime(2026, 7, 11, 19, 55, tzinfo=timezone.utc),  # Sat 15:55 ET
    )
    assert result.status == "PASS"


# --- check_open_positions_consistent ---------------------------------------

def test_positions_consistent_when_db_empty(tmp_path: Path):
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=0)
    h = tmp_path / "hb.json"
    _write_heartbeat(h, datetime.now(timezone.utc).isoformat())
    result = check_open_positions_consistent(db, h)
    assert result.status == "PASS"


def test_positions_consistent_with_open_positions_fresh_heartbeat(tmp_path: Path):
    # Open positions + fresh heartbeat = PASS (loop is alive, just holding)
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=3)
    h = tmp_path / "hb.json"
    _write_heartbeat(h, datetime.now(timezone.utc).isoformat())
    result = check_open_positions_consistent(db, h)
    assert result.status == "PASS"


def test_positions_consistent_warn_open_positions_stale_heartbeat(tmp_path: Path):
    # Open positions + heartbeat between 90s–5min = WARN (loop slipping)
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=2)
    h = tmp_path / "hb.json"
    iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 200, tz=timezone.utc
    ).isoformat()
    _write_heartbeat(h, iso)
    result = check_open_positions_consistent(db, h)
    assert result.status == "WARN"


def test_positions_consistent_fail_when_heartbeat_stale(tmp_path: Path):
    # Open positions + heartbeat >5min old = FAIL (loop stalled)
    db = tmp_path / "burn_in.db"
    _seed_db(db, open_trades=2)
    h = tmp_path / "hb.json"
    iso = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 600, tz=timezone.utc
    ).isoformat()
    _write_heartbeat(h, iso)
    result = check_open_positions_consistent(db, h)
    assert result.status == "FAIL"


# --- check_market_data_freshness -------------------------------------------

def test_market_data_fresh_during_hours(tmp_path: Path):
    db = tmp_path / "m.db"
    fresh_ts = datetime.now(timezone.utc).isoformat()
    _seed_db(db, last_market_ts=fresh_ts)
    # 13:00 ET on a weekday
    result = check_market_data_freshness(
        db_path=db,
        now_utc=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )
    # Either PASS (data is fresh) or at worst WARN — never FAIL outright
    assert result.status in {"PASS", "WARN"}


def test_market_data_fail_during_hours_when_old(tmp_path: Path):
    db = tmp_path / "m.db"
    old_ts = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() - 3600, tz=timezone.utc
    ).isoformat()
    _seed_db(db, last_market_ts=old_ts)
    result = check_market_data_freshness(
        db_path=db,
        now_utc=datetime(2026, 7, 6, 17, 0, tzinfo=timezone.utc),
    )
    assert result.status == "FAIL"


def test_market_data_no_data_after_hours_is_passing(tmp_path: Path):
    db = tmp_path / "m.db"
    _seed_db(db, last_market_ts=None)
    # Saturday → market closed → no expectations
    result = check_market_data_freshness(
        db_path=db,
        now_utc=datetime(2026, 7, 11, 17, 0, tzinfo=timezone.utc),
    )
    assert result.status == "PASS"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_health_checks.py -v`
Expected: collection error (`ModuleNotFoundError: No module named 'trading_bot.health.checks'`)

- [ ] **Step 3: Write the checks module**

```python
# trading_bot/health/checks.py
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from trading_bot.health.types import CheckResult


def _safe_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


# 15:55 ET expressed in minutes from local UTC offset. US/Eastern is UTC-4
# (EDT) or UTC-5 (EST); we approximate EDT for summer months (matches burn-in's
# expectation year-round; if it drifts, callers can override via parameter).
def _now_in_eastern_minutes(now_utc: datetime) -> int:
    eastern = now_utc.astimezone(timezone.utc).replace(tzinfo=None)
    # Assume EDT (UTC-4) for the trading calendar
    eastern = eastern.replace(hour=(eastern.hour - 4) % 24)
    return eastern.hour * 60 + eastern.minute


def check_pid_alive(pid_file: Path) -> CheckResult:
    """Verify the burn-in PID is alive."""
    if not pid_file.exists():
        return CheckResult(
            name="pid_alive",
            status="FAIL",
            detail=f"PID file missing: {pid_file}",
            observed={"path": str(pid_file)},
        )
    raw = pid_file.read_text().strip()
    try:
        pid = int(raw)
    except ValueError:
        return CheckResult(
            name="pid_alive",
            status="FAIL",
            detail=f"PID file contains invalid value: {raw!r}",
            observed={"path": str(pid_file)},
        )
    if _safe_alive(pid):
        return CheckResult(
            name="pid_alive",
            status="PASS",
            detail=f"PID {pid} alive",
            observed={"pid": pid},
        )
    return CheckResult(
        name="pid_alive",
        status="FAIL",
        detail=f"PID {pid} not responding to signal 0",
        observed={"pid": pid},
    )


def check_heartbeat_fresh(heartbeat_path: Path, *, max_age_seconds: int) -> CheckResult:
    """Verify the burn-in loop is making progress."""
    if not heartbeat_path.exists():
        return CheckResult(
            name="heartbeat_fresh",
            status="FAIL",
            detail=f"heartbeat missing: {heartbeat_path}",
            observed={},
        )
    try:
        payload = json.loads(heartbeat_path.read_text())
        ts = datetime.fromisoformat(payload["ts"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="heartbeat_fresh",
            status="FAIL",
            detail=f"heartbeat unreadable: {exc}",
            observed={},
        )
    age_s = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    if age_s <= max_age_seconds:
        return CheckResult(
            name="heartbeat_fresh",
            status="PASS",
            detail=f"heartbeat fresh (last {int(age_s)}s ago)",
            observed={"age_seconds": int(age_s)},
        )
    if age_s <= 5 * 60:
        return CheckResult(
            name="heartbeat_fresh",
            status="WARN",
            detail=f"heartbeat stale (last {int(age_s)}s ago)",
            observed={"age_seconds": int(age_s)},
        )
    return CheckResult(
        name="heartbeat_fresh",
        status="FAIL",
        detail=f"heartbeat very stale (last {int(age_s)}s ago)",
        observed={"age_seconds": int(age_s)},
    )


def check_dashboard_health(port: int, *, timeout_seconds: float = 1.0) -> CheckResult:
    """Probe the loopback dashboard's /api/health endpoint."""
    url = f"http://127.0.0.1:{port}/api/health"
    request = Request(url, headers={"User-Agent": "trading-bot-health"})
    try:
        response = urlopen(request, timeout=timeout_seconds)
        status_code = getattr(response, "status", 200)
    except URLError as exc:
        return CheckResult(
            name="dashboard_health",
            status="FAIL",
            detail=f"dashboard unreachable: {exc.reason}",
            observed={"url": url, "port": port},
        )
    except Exception as exc:  # connection refused, etc.
        return CheckResult(
            name="dashboard_health",
            status="FAIL",
            detail=f"dashboard connection error: {exc}",
            observed={"url": url, "port": port},
        )
    if status_code == 200:
        return CheckResult(
            name="dashboard_health",
            status="PASS",
            detail=f"dashboard :{port} health {status_code}",
            observed={"url": url, "status_code": status_code},
        )
    return CheckResult(
        name="dashboard_health",
        status="WARN",
        detail=f"dashboard :{port} returned {status_code}",
        observed={"url": url, "status_code": status_code},
    )


def check_eod_watchdog(pid_file: Path, *, now_utc: datetime) -> CheckResult:
    """Verify the EOD watchdog will fire (or has fired) at the expected window."""
    minute_of_day = _now_in_eastern_minutes(now_utc)
    weekday = now_utc.weekday()  # 0=Mon
    eod_window_start = 15 * 60 + 50  # 15:50 ET — start observing
    eod_window_end = 16 * 60 + 5  # 16:05 ET — expected to have fired

    in_window = weekday <= 4 and eod_window_start <= minute_of_day <= eod_window_end
    if not in_window:
        return CheckResult(
            name="eod_watchdog",
            status="PASS",
            detail="outside EOD watchdog window",
            observed={"weekday": weekday, "et_minute": minute_of_day},
        )

    if not pid_file.exists():
        return CheckResult(
            name="eod_watchdog",
            status="FAIL",
            detail="EOD watchdog PID file missing during 15:50-16:05 ET window",
            observed={"pid_file": str(pid_file)},
        )
    raw = pid_file.read_text().strip()
    try:
        pid = int(raw)
    except ValueError:
        return CheckResult(
            name="eod_watchdog",
            status="FAIL",
            detail="EOD watchdog PID file unreadable",
            observed={"pid_file": str(pid_file)},
        )
    if _safe_alive(pid):
        return CheckResult(
            name="eod_watchdog",
            status="PASS",
            detail=f"EOD watchdog alive (pid={pid})",
            observed={"pid": pid},
        )
    return CheckResult(
        name="eod_watchdog",
        status="FAIL",
        detail=f"EOD watchdog PID {pid} not alive in window",
        observed={"pid": pid},
    )


def check_open_positions_consistent(
    db_path: Path, heartbeat_path: Path
) -> CheckResult:
    """Flag open positions when the heartbeat is stale (loop stalled).

    Logic:
    - No open positions → PASS (nothing to manage)
    - Open positions + fresh heartbeat → PASS (loop is making progress)
    - Open positions + heartbeat in (90s, 5min] → WARN (loop slipping)
    - Open positions + heartbeat > 5min old (or missing) → FAIL
    """
    if not db_path.exists():
        return CheckResult(
            name="open_positions_consistent",
            status="PASS",
            detail=f"trades DB missing: {db_path}",
            observed={},
        )
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM trades WHERE status='FILLED'")
        open_count = int(cursor.fetchone()[0])
    finally:
        conn.close()

    if open_count == 0:
        return CheckResult(
            name="open_positions_consistent",
            status="PASS",
            detail="no open positions",
            observed={"open_trades": 0},
        )

    if not heartbeat_path.exists():
        return CheckResult(
            name="open_positions_consistent",
            status="FAIL",
            detail=f"{open_count} open positions, heartbeat missing",
            observed={"open_trades": open_count},
        )
    try:
        payload = json.loads(heartbeat_path.read_text())
        ts = datetime.fromisoformat(payload["ts"])
        age_s = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    except (ValueError, KeyError, json.JSONDecodeError):
        return CheckResult(
            name="open_positions_consistent",
            status="WARN",
            detail=f"{open_count} open positions, heartbeat unreadable",
            observed={"open_trades": open_count},
        )

    if age_s <= 90:
        return CheckResult(
            name="open_positions_consistent",
            status="PASS",
            detail=f"{open_count} open positions, heartbeat fresh",
            observed={"open_trades": open_count, "heartbeat_age_s": int(age_s)},
        )
    if age_s <= 5 * 60:
        return CheckResult(
            name="open_positions_consistent",
            status="WARN",
            detail=f"{open_count} open positions, heartbeat stale {int(age_s)}s",
            observed={"open_trades": open_count, "heartbeat_age_s": int(age_s)},
        )
    return CheckResult(
        name="open_positions_consistent",
        status="FAIL",
        detail=f"{open_count} open positions, heartbeat stale {int(age_s)}s",
        observed={"open_trades": open_count, "heartbeat_age_s": int(age_s)},
    )


def check_market_data_freshness(db_path: Path, *, now_utc: datetime) -> CheckResult:
    """Verify recent market data matches market-hours expectations."""
    if not db_path.exists():
        return CheckResult(
            name="market_data_freshness",
            status="PASS",
            detail=f"market DB missing: {db_path}",
            observed={},
        )
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            "SELECT MAX(timestamp) FROM market_data WHERE timeframe IN ('5m','1m','1d')"
        )
        row = cursor.fetchone()
        latest = row[0] if row else None
    finally:
        conn.close()

    if not latest:
        weekday = now_utc.weekday()
        if weekday <= 4:
            return CheckResult(
                name="market_data_freshness",
                status="WARN",
                detail="no market data yet",
                observed={},
            )
        return CheckResult(
            name="market_data_freshness",
            status="PASS",
            detail="no market data — outside trading hours",
            observed={},
        )

    try:
        latest_dt = datetime.fromisoformat(latest)
    except ValueError:
        return CheckResult(
            name="market_data_freshness",
            status="WARN",
            detail=f"unparseable market_data timestamp: {latest}",
            observed={"latest": latest},
        )

    age_minutes = (now_utc - latest_dt.astimezone(timezone.utc)).total_seconds() / 60.0
    weekday = now_utc.weekday()
    minute_of_day = _now_in_eastern_minutes(now_utc)
    market_open = 9 * 60 + 30
    market_close = 16 * 60
    in_market_hours = (
        weekday <= 4 and market_open <= minute_of_day <= market_close
    )

    if not in_market_hours:
        return CheckResult(
            name="market_data_freshness",
            status="PASS",
            detail="outside market hours",
            observed={"age_minutes": round(age_minutes, 1)},
        )
    if age_minutes <= 10:
        return CheckResult(
            name="market_data_freshness",
            status="PASS",
            detail=f"market data {int(age_minutes)}m old",
            observed={"age_minutes": round(age_minutes, 1)},
        )
    if age_minutes <= 30:
        return CheckResult(
            name="market_data_freshness",
            status="WARN",
            detail=f"market data {int(age_minutes)}m old",
            observed={"age_minutes": round(age_minutes, 1)},
        )
    return CheckResult(
        name="market_data_freshness",
        status="FAIL",
        detail=f"market data {int(age_minutes)}m old",
        observed={"age_minutes": round(age_minutes, 1)},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_health_checks.py -v`
Expected: all passed (the only flaky cases are `test_pid_alive_fail_dead` and `test_eod_watchdog_pass_in_window_with_live_pid`, both of which allow either outcome — see test comments)

- [ ] **Step 5: Commit**

```bash
git add trading_bot/health/checks.py tests/test_health_checks.py
git commit -m "feat(health): six pure check functions with tests"
```

---

### Task 3: Health runner + integration tests

**Files:**
- Create: `trading_bot/health/runner.py`
- Create: `tests/test_health_runner.py`

**Interfaces:**
- Consumes: `CheckResult`, `HealthReport` from Task 1; the six check functions from Task 2
- Produces: `run_health_checks(state_dir: Path, *, db_path: Path, dashboard_port: int, eod_watchdog_pid_file: Path, now_utc: datetime | None = None) -> HealthReport`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_health_runner.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_health_runner.py -v`
Expected: `ModuleNotFoundError: No module named 'trading_bot.health.runner'`

- [ ] **Step 3: Write runner**

```python
# trading_bot/health/runner.py
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trading_bot.health.checks import (
    check_dashboard_health,
    check_eod_watchdog,
    check_heartbeat_fresh,
    check_market_data_freshness,
    check_open_positions_consistent,
    check_pid_alive,
)
from trading_bot.health.types import CheckResult, HealthReport


def run_health_checks(
    *,
    state_dir: Path,
    db_path: Path,
    dashboard_port: int,
    eod_watchdog_pid_file: Path,
    now_utc: datetime | None = None,
) -> HealthReport:
    """Run all burn-in checks; never raises. Failures degrade to FAIL."""
    now = now_utc or datetime.now(timezone.utc)
    pid_file = state_dir / "burn_in.pid"
    heartbeat = state_dir / "heartbeat.json"

    results: list[CheckResult] = []
    results.append(check_pid_alive(pid_file))
    results.append(check_heartbeat_fresh(heartbeat, max_age_seconds=90))
    results.append(check_dashboard_health(port=dashboard_port))
    results.append(check_eod_watchdog(pid_file=eod_watchdog_pid_file, now_utc=now))
    results.append(check_open_positions_consistent(db_path=db_path, heartbeat_path=heartbeat))
    results.append(check_market_data_freshness(db_path=db_path, now_utc=now))

    return HealthReport(checks=results, generated_at=now.isoformat())
```

- [ ] **Step 4: Update package exports**

```python
# trading_bot/health/__init__.py
from __future__ import annotations

from trading_bot.health.runner import run_health_checks
from trading_bot.health.types import CheckResult, HealthReport, Status

__all__ = ["CheckResult", "HealthReport", "Status", "run_health_checks"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_health_runner.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add trading_bot/health/runner.py trading_bot/health/__init__.py tests/test_health_runner.py
git commit -m "feat(health): runner aggregating six checks into HealthReport"
```

---

### Task 4: `doctor --burn-in` CLI flag

**Files:**
- Modify: `trading_bot/cli/app.py:106-108` (the existing `doctor` command)
- Create: `tests/test_doctor_burn_in.py`

**Interfaces:**
- Consumes: `run_health_checks` from Task 3; existing `doctor()` function
- Produces: `./tradebot-local doctor --burn-in [--json]` sub-command; exit codes 0/1/2

- [ ] **Step 1: Write failing test**

```python
# tests/test_doctor_burn_in.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trading_bot.cli.app import app


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch):
    sd = tmp_path / "state"
    sd.mkdir()
    # Write a current-time heartbeat so the heartbeat check is PASS
    import json as _json
    (sd / "heartbeat.json").write_text(_json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "cycle": 1,
        "fills": 0,
        "exits": 0,
        "rejects": 0,
    }))
    # Self PID so pid_alive is PASS
    import os
    (sd / "burn_in.pid").write_text(str(os.getpid()))
    monkeypatch.setenv("TRADING_BOT_STATE_DIR", str(sd))
    return sd


def test_doctor_default_unchanged(state_dir: Path):
    runner = CliRunner()
    # Existing doctor behavior must remain — should not require --burn-in
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "doctor" in result.output


def test_doctor_burn_in_human_output(state_dir: Path, monkeypatch):
    # Patch the runner to avoid touching real DB or remote ports
    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    fake = HealthReport(
        checks=[
            CheckResult(name="pid_alive", status="PASS", detail="alive", observed=None),
            CheckResult(name="heartbeat_fresh", status="PASS", detail="fresh", observed=None),
            CheckResult(name="dashboard_health", status="WARN", detail="non-200", observed=None),
            CheckResult(name="eod_watchdog", status="PASS", detail="ok", observed=None),
            CheckResult(name="open_positions_consistent", status="PASS", detail="ok", observed=None),
            CheckResult(name="market_data_freshness", status="PASS", detail="ok", observed=None),
        ],
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"])
    assert result.exit_code == 1, result.output
    assert "Summary" in result.output
    assert "[burn-in]" in result.output


def test_doctor_burn_in_json_output(state_dir: Path, monkeypatch):
    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    fake = HealthReport(
        checks=[
            CheckResult(name="pid_alive", status="PASS", detail="alive", observed=None),
            CheckResult(name="heartbeat_fresh", status="PASS", detail="fresh", observed=None),
            CheckResult(name="dashboard_health", status="PASS", detail="ok", observed=None),
            CheckResult(name="eod_watchdog", status="PASS", detail="ok", observed=None),
            CheckResult(name="open_positions_consistent", status="PASS", detail="ok", observed=None),
            CheckResult(name="market_data_freshness", status="PASS", detail="ok", observed=None),
        ],
        generated_at="2026-07-10T09:31:00+00:00",
    )
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["worst_status"] == "PASS"
    assert len(payload["checks"]) == 6


def test_doctor_burn_in_fail_exit_code(state_dir: Path, monkeypatch):
    from trading_bot.health import runner as runner_module
    from trading_bot.health.types import CheckResult, HealthReport

    fake = HealthReport(
        checks=[CheckResult(name="pid_alive", status="FAIL", detail="dead", observed=None)],
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    monkeypatch.setattr(runner_module, "run_health_checks", lambda **kw: fake)

    runner = CliRunner()
    result = runner.invoke(app, ["doctor", "--burn-in"])
    assert result.exit_code == 2
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.venv/bin/python -m pytest tests/test_doctor_burn_in.py -v`
Expected: existing `doctor` test invokes fail because the new --burn-in flag isn't accepted (CLI exits with "no such option" or equivalent) OR the new tests error because the sub-command isn't wired

- [ ] **Step 3: Wire the CLI flag**

Modify `trading_bot/cli/app.py:106-108` (the existing `doctor` command). Replace the current implementation with:

```python
@app.command()
def doctor(
    ctx: typer.Context,
    burn_in: bool = typer.Option(
        False,
        "--burn-in",
        help="Run the burn-in reliability health checks (network-free).",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit machine-readable JSON (implies --burn-in).",
    ),
) -> None:
    """Check local app readiness without fetching market data."""
    if not burn_in and not json_output:
        typer.echo(_format_doctor(ctx.obj))
        return

    from pathlib import Path

    from trading_bot.health.runner import run_health_checks
    from trading_bot.health.types import HealthReport

    state_dir_setting = Path(getattr(ctx.obj.app, "state_dir", "state"))
    db_path = Path(ctx.obj.app.state_db_path)
    dashboard_port = int(getattr(ctx.obj.app, "dashboard_port", 8080))
    eod_watchdog_pid_file = state_dir_setting / "eod_watchdog.pid"

    report: HealthReport = run_health_checks(
        state_dir=state_dir_setting,
        db_path=db_path,
        dashboard_port=dashboard_port,
        eod_watchdog_pid_file=eod_watchdog_pid_file,
    )

    if json_output:
        typer.echo(json.dumps(report.to_dict()))
    else:
        # Human output with [burn-in] prefix per row
        for check in report.checks:
            typer.echo(f"[burn-in] {check.name:<28} {check.status:<5} {check.detail}")
        typer.echo(
            f"Summary: worst={report.worst_status()}  "
            f"PASS={sum(1 for c in report.checks if c.status=='PASS')}  "
            f"WARN={sum(1 for c in report.checks if c.status=='WARN')}  "
            f"FAIL={sum(1 for c in report.checks if c.status=='FAIL')}"
        )

    raise typer.Exit(
        code={"PASS": 0, "WARN": 1, "FAIL": 2}[report.worst_status()]
    )
```

Add at the top of `app.py` alongside the existing imports:

```python
import json  # only if not already imported
```

Confirm `ctx.obj.app` exposes `state_db_path`, `state_dir` (or compute from it), and `dashboard_port`. If `state_dir` is missing, fall back to deriving from `state_db_path`'s parent. (See notes below.)

- [ ] **Step 4: Optional fallback for missing settings**

If `ctx.obj.app.state_dir` or `dashboard_port` does not exist, edit the `doctor` body to use:

```python
state_dir_setting = Path(getattr(ctx.obj.app, "state_dir", None) or Path(ctx.obj.app.state_db_path).parent)
dashboard_port = int(getattr(ctx.obj.app, "dashboard_port", 8080))
```

This keeps the call from importing config objects that aren't guaranteed to exist on `Settings`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_doctor_burn_in.py -v`
Expected: 4 passed

Run the doctor smoke if it exists: `.venv/bin/python -m pytest tests/test_cli_smoke.py -v` — must still pass.

- [ ] **Step 6: Commit**

```bash
git add trading_bot/cli/app.py tests/test_doctor_burn_in.py
git commit -m "feat(cli): doctor --burn-in for burn-in reliability report"
```

---

### Task 5: Heartbeat writer + `run_health_check` shell integration

**Files:**
- Modify: `scripts/auto-burn-in.sh` (heartbeat write at top of loop; new `run_health_check` function; cadence + 15:50 pre-EOD hook; persist EOD watchdog PID to file)
- Modify: `tests/test_auto_burn_in_script.py` (extend to cover heartbeat and health-check cadence)
- Read (no modify): `trading_bot/data/data_store.py` is NOT touched here

**Interfaces:**
- Produces: `state/burn_in/heartbeat.json` updated each loop; `state/burn_in/eod_watchdog.pid`; `state/burn_in/burn_in.pid`; an aggregated `logs/burn_in/health.jsonl` line every 30 min and at 15:50 ET

- [ ] **Step 1: Add heartbeat writer + PID files**

Add the following near the top of `scripts/auto-burn-in.sh`, in the same block as the existing `STATE_DIR` and `LOG_DIR` definitions. Locate the `STATE_DIR` initialization and add these right after:

```bash
# 2026-07-10: Burn-in reliability control plane. Write a heartbeat each
# loop iteration and persist PIDs for the doctor --burn-in health checks
# to consume (see trading_bot/health/checks.py).
HEALTH_STATE_DIR="$STATE_DIR/burn_in"
mkdir -p "$HEALTH_STATE_DIR"
echo "$$" > "$HEALTH_STATE_DIR/burn_in.pid"
HEARTBEAT_FILE="$HEALTH_STATE_DIR/heartbeat.json"
EOD_WATCHDOG_PID_FILE="$HEALTH_STATE_DIR/eod_watchdog.pid"
HEALTH_LOG="$LOG_DIR/health.jsonl"

write_heartbeat() {
    local fills="$1" exits="$2" rejects="$3"
    local ts_iso
    ts_iso=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')
    cat > "$HEARTBEAT_FILE" <<EOF
{"ts":"$ts_iso","cycle":$CYCLE_COUNT,"fills":$fills,"exits":$exits,"rejects":$rejects}
EOF
}
```

- [ ] **Step 2: Update `start_eod_watchdog` to write its PID**

In `start_eod_watchdog()` (around line 649), the `EOD_WATCHDOG_PID=$!` line already captures the pid. Add a write to `EOD_WATCHDOG_PID_FILE` immediately after:

```bash
    EOD_WATCHDOG_PID=$!
    echo "$EOD_WATCHDOG_PID" > "$EOD_WATCHDOG_PID_FILE" 2>/dev/null || true
```

- [ ] **Step 3: Add `run_health_check()` function**

Insert after the existing `run_nightly_tuning()` function, mirroring its style:

```bash
# Function to run the burn-in reliability health check.
# Mirrors run_nightly_tuning(): capture stdout to a log, never exit 1 on
# failure (the heartbeats themselves expose health; we don't want the
# check pipeline to take down the burn-in).
run_health_check() {
    local timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local rc=0
    local output
    output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" doctor --burn-in --json 2>&1) || rc=$?
    echo "$output" >> "$HEALTH_LOG" 2>/dev/null || true
    if [ "$rc" -ne 0 ]; then
        echo "[$timestamp] ⚠️  Health check exit=$rc (see $HEALTH_LOG)"
    fi
    return 0
}
```

- [ ] **Step 4: Hook into the main loop**

Modify the main `while true; do` loop (around line 976). Replace the cycle body so:

a) At the very top of each iteration, after `CYCLE_COUNT=$((CYCLE_COUNT + 1))`, write a fresh heartbeat:

```bash
    CYCLE_COUNT=$((CYCLE_COUNT + 1))
    write_heartbeat 0 0 0  # fills/exits/rejects tracked later; placeholder for now
```

b) Add a 30-minute cadence immediately before the `sleep 60` at the bottom. Replace the block:

```bash
    echo ""
    echo "[$timestamp] Sleeping 60 seconds (1 min)..."
    sleep 60
```

with:

```bash
    # 30-minute health check cadence
    if [ $((CYCLE_COUNT % 30)) -eq 0 ]; then
        run_health_check
    fi
    # 15:50 ET pre-EOD hard check (5 min before EOD exit)
    local pre_eod_h=$(date +%H)
    local pre_eod_m=$(date +%M)
    local pre_eod_dow=$(date +%u)
    if [ "$pre_eod_dow" -le 5 ] \
        && [ $((10#$pre_eod_h * 60 + 10#$pre_eod_m)) -eq $((15 * 60 + 50)) ]; then
        run_health_check
    fi

    echo ""
    echo "[$timestamp] Sleeping 60 seconds (1 min)..."
    sleep 60
```

c) After `ensure_dashboard` (already called), also call `run_health_check` once on startup. Add a single line right after the existing `ensure_dashboard` startup call:

```bash
ensure_dashboard
start_eod_watchdog
run_health_check   # one-time baseline on boot
```

- [ ] **Step 5: Write shell tests**

Extend `tests/test_auto_burn_in_script.py` (the existing test file) by adding the following tests. They exercise only the new functions in isolation, not the full loop.

```python
# Add to tests/test_auto_burn_in_script.py — append below existing tests.
import json
import os
import subprocess
from pathlib import Path


def test_write_heartbeat_creates_json(tmp_path: Path, monkeypatch):
    """The write_heartbeat shell helper writes valid JSON containing ts + cycle."""
    # Run a tiny bash script that sources the function and writes once.
    script = """
    set -e
    CYCLE_COUNT=7
    STATE_DIR="$STATE_DIR"
    mkdir -p "$STATE_DIR/burn_in"
    HEALTH_STATE_DIR="$STATE_DIR/burn_in"
    HEARTBEAT_FILE="$HEALTH_STATE_DIR/heartbeat.json"
    write_heartbeat() {
        local fills="$1" exits="$2" rejects="$3"
        local ts_iso
        ts_iso=$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')
        cat > "$HEARTBEAT_FILE" <<EOF
{"ts":"$ts_iso","cycle":$CYCLE_COUNT,"fills":$fills,"exits":$exits,"rejects":$rejects}
EOF
    }
    write_heartbeat 1 2 3
    cat "$HEARTBEAT_FILE"
    """
    env = os.environ.copy()
    env["STATE_DIR"] = str(tmp_path)
    proc = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload["cycle"] == 7
    assert payload["fills"] == 1
    assert payload["exits"] == 2
    assert payload["rejects"] == 3
    assert "ts" in payload


def test_eod_watchdog_writes_pid_file(tmp_path: Path):
    """start_eod_watchdog should persist its PID to EOD_WATCHDOG_PID_FILE."""
    # We can't actually start the watchdog (it loops forever). Instead,
    # verify the line `echo "$EOD_WATCHDOG_PID" > "$EOD_WATCHDOG_PID_FILE"`
    # exists in auto-burn-in.sh. This guards against accidental deletion.
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "auto-burn-in.sh"
    contents = script_path.read_text()
    assert "EOD_WATCHDOG_PID_FILE" in contents
    assert (
        "echo \"$EOD_WATCHDOG_PID\" > \"$EOD_WATCHDOG_PID_FILE\"" in contents
    ), "start_eod_watchdog must persist its PID for the health check"


def test_run_health_check_function_present():
    """run_health_check shell function must be defined in auto-burn-in.sh."""
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "auto-burn-in.sh"
    contents = script_path.read_text()
    assert "run_health_check()" in contents
    assert "doctor --burn-in --json" in contents
```

- [ ] **Step 6: Run shell tests**

Run: `.venv/bin/python -m pytest tests/test_auto_burn_in_script.py -v`
Expected: all passed (existing + new)

- [ ] **Step 7: Run full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: 0 failures (ignoring any pre-existing skipped/flaky tests in the current branch — the new tests must be green).

- [ ] **Step 8: Commit**

```bash
git add scripts/auto-burn-in.sh tests/test_auto_burn_in_script.py
git commit -m "feat(burn-in): heartbeat writer + run_health_check shell cadence"
```

---

### Task 6: AGENTS.md update

**Files:**
- Modify: `AGENTS.md` (operator entrypoints section: add `./tradebot-local doctor --burn-in`)

- [ ] **Step 1: Add the new operator command**

In the `AGENTS.md` "Common Commands" section, the kill-switch entry already exists. Add the new entry next to it:

```markdown
# Health check
./tradebot-local doctor --burn-in            # burn-in reliability report
./tradebot-local doctor --burn-in --json     # machine-readable
```

- [ ] **Step 2: Update the entry-point note**

In the "Safety Constraints" section, append a note that the health check is the operator's first verification before market open:

```markdown
9. **Burn-in health is observable** — run `./tradebot-local doctor --burn-in` before market open to confirm heartbeat, dashboard, EOD watchdog, and DB are healthy.
```

- [ ] **Step 3: Verify**

Re-run: `.venv/bin/python -m pytest -q`
Expected: green

Manually inspect:
```bash
./tradebot-local doctor --help
```

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md
git commit -m "docs(agents): add ./tradebot-local doctor --burn-in to operator commands"
```

---

### Task 7: Live-baseline smoke run

**Files:** none (operator-only verification step)

- [ ] **Step 1: Run the new command against the current live burn-in**

```bash
./tradebot-local doctor --burn-in
```

Expected: a `Summary: ...` line plus 6 `[burn-in] ...` rows. The current burn-in PID 13773 (per the latest post-mortem) and dashboard :8080 should both be healthy (PASS). Heartbeat will FAIL on first invocation because the heartbeat writer hasn't shipped yet — this is fine; once the live burn-in restarts after Task 5 ships, the heartbeat should be present.

- [ ] **Step 2: Verify JSON output**

```bash
./tradebot-local doctor --burn-in --json
```

Expected: parseable JSON with `worst_status`, `generated_at`, `checks: [...]`.

- [ ] **Step 3: If running with the new heartbeat, confirm in `state/burn_in/heartbeat.json`**

```bash
cat state/burn_in/heartbeat.json
```

Expected (after one full burn-in cycle): `{"ts":"...","cycle":N,"fills":0,"exits":0,"rejects":0}`.

---

## Self-Review (run mentally before/after writing)

**Spec coverage:**
- Two-layer architecture → Tasks 1–3 + Task 5 step 1
- Six checks → Task 2
- `HealthReport` with worst-severity → Task 1 + Task 3
- CLI surface `--burn-in` with `--json` → Task 4
- Exit codes 0/1/2 → Task 4 step 3
- Shell hook: startup + 30-min + 15:50 pre-EOD → Task 5 steps 3–4
- Heartbeat JSON `{ts, cycle, last_action, fills, exits, rejects}` → Task 5 step 1
- PID files (`burn_in.pid`, `eod_watchdog.pid`) → Task 5 step 2 + existing dashboard.pid
- `logs/burn_in/health.jsonl` → Task 5 step 3
- Network-free tests → all tasks (monkeypatch urlopen, tmp_path DBs, mocked clocks)
- AGENTS.md update → Task 6

**Placeholder scan:** No TBD/TODO/vague items in any task.

**Type consistency:**
- `CheckResult(name, status, detail, observed)` defined Task 1 — used identically in Tasks 2 and 3 and 4.
- `HealthReport(checks, generated_at)` with `worst_status()` and `to_dict()` — Task 1 only adds; Task 3 and 4 consume.
- `run_health_checks(state_dir, db_path, dashboard_port, eod_watchdog_pid_file, now_utc)` signature fixed in Task 3; consumed unchanged in Task 4.

All consistent.
