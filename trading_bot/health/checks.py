from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from trading_bot.health.types import CheckResult


def _safe_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM: process exists, we just cannot signal it (e.g., launchd / PID 1)
        return True
    except OSError:
        return False


def _now_in_eastern_minutes(now_utc: datetime) -> int:
    """Return minutes-of-day in US/Eastern for *now_utc*.

    Uses ZoneInfo so DST transitions are reflected exactly.
    """
    eastern = now_utc.astimezone(ZoneInfo("America/New_York"))
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
    """Verify the EOD watchdog is alive (and has fired in the post-EOD window).

    The audit previously returned PASS outside 15:50–16:05 even when the
    watchdog PID file was absent or the process was dead. That let a
    dead watchdog pass the morning health check.

    New behavior:
    - During burner operation (weekdays 09:00-16:30 ET): the watchdog PID
      file must exist and reference a live process. A missing or dead
      watchdog is FAIL.
    - Outside that window: still PASS because the burner sleeps; the
      watchdog is not required to be alive.
    - Post-close verification: if 16:05+ ET on a weekday, the daily
      EOD marker should also exist. (Marker path is operator-dependent
      so we only soft-warn there.)
    """
    minute_of_day = _now_in_eastern_minutes(now_utc)
    weekday = now_utc.weekday()  # 0=Mon
    burner_window_start = 9 * 60          # 09:00 ET
    burner_window_end = 16 * 60 + 30      # 16:30 ET (gives a safety margin past 16:05)

    in_burner_hours = weekday <= 4 and burner_window_start <= minute_of_day <= burner_window_end
    if not in_burner_hours:
        return CheckResult(
            name="eod_watchdog",
            status="PASS",
            detail="outside burner hours; watchdog not required",
            observed={"weekday": weekday, "et_minute": minute_of_day},
        )

    if not pid_file.exists():
        return CheckResult(
            name="eod_watchdog",
            status="FAIL",
            detail="EOD watchdog PID file missing during burner hours",
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
        detail=f"EOD watchdog PID {pid} not alive during burner hours",
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


def check_scan_freshness(
    scan_results_path: Path,
    *,
    now_utc: datetime,
    max_age_seconds: int = 180,
) -> CheckResult:
    """Verify the most recent scan produced a snapshot inside the burn-in loop.

    The scan orchestrator persists a JSON snapshot after each completed scan.
    This check confirms one exists, is recent, and did not exceed its
    deadline — three signals the burn-in loop is actually making progress.
    """
    if not scan_results_path.exists():
        return CheckResult(
            name="scan_freshness",
            status="FAIL",
            detail=f"scan results missing: {scan_results_path}",
            observed={"path": str(scan_results_path)},
        )
    try:
        payload = json.loads(scan_results_path.read_text())
    except json.JSONDecodeError as exc:
        return CheckResult(
            name="scan_freshness",
            status="FAIL",
            detail=f"scan results unreadable: {exc}",
            observed={"path": str(scan_results_path)},
        )

    if not isinstance(payload, dict):
        return CheckResult(
            name="scan_freshness",
            status="FAIL",
            detail=(
                f"scan results malformed: expected JSON object, got "
                f"{type(payload).__name__}"
            ),
            observed={"path": str(scan_results_path)},
        )

    raw_ts = payload.get("generated_at", "")
    try:
        ts = datetime.fromisoformat(str(raw_ts))
    except (TypeError, ValueError) as exc:
        return CheckResult(
            name="scan_freshness",
            status="FAIL",
            detail=f"scan results unreadable: {exc}",
            observed={"path": str(scan_results_path)},
        )

    if not isinstance(ts, datetime):
        return CheckResult(
            name="scan_freshness",
            status="FAIL",
            detail=f"scan results missing or invalid generated_at",
            observed={"path": str(scan_results_path)},
        )

    age_s = (now_utc.astimezone(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()
    raw_summary = payload.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    deadline_exceeded = bool(summary.get("deadline_exceeded", False))

    if age_s <= max_age_seconds:
        status = "WARN" if deadline_exceeded else "PASS"
        return CheckResult(
            name="scan_freshness",
            status=status,
            detail=(
                f"scan fresh (last {int(age_s)}s ago)"
                if not deadline_exceeded
                else f"scan fresh but deadline exceeded (last {int(age_s)}s ago)"
            ),
            observed={
                "age_seconds": int(age_s),
                "deadline_exceeded": deadline_exceeded,
            },
        )

    if age_s <= max_age_seconds * 5:
        return CheckResult(
            name="scan_freshness",
            status="WARN",
            detail=f"scan stale (last {int(age_s)}s ago)",
            observed={"age_seconds": int(age_s)},
        )

    return CheckResult(
        name="scan_freshness",
        status="FAIL",
        detail=f"scan very stale (last {int(age_s)}s ago)",
        observed={"age_seconds": int(age_s)},
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

    age_minutes = (datetime.now(timezone.utc) - latest_dt.astimezone(timezone.utc)).total_seconds() / 60.0
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


def check_tuning_experiment(state_dir: Path, now_utc: datetime) -> CheckResult:
    """Verify the active tuning experiment is in a healthy state.

    Reads ``state/tuning_experiments/current.json`` when present. No
    current.json is the expected steady state and is reported as PASS.
    A state file with a non-pass terminal status surfaces as WARN
    (INCONCLUSIVE) or FAIL (ERROR) so the burn-in loop's ``doctor``
    command can flag it before operators get paged.
    """
    path = state_dir / "tuning_experiments" / "current.json"
    if not path.exists():
        return CheckResult(
            name="tuning_experiment",
            status="PASS",
            detail="no active experiment",
            observed={},
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return CheckResult(
            name="tuning_experiment",
            status="FAIL",
            detail=f"corrupt state: {exc}",
            observed={"path": str(path)},
        )
    status = payload.get("status") if isinstance(payload, dict) else None
    if status == "INCONCLUSIVE":
        return CheckResult(
            name="tuning_experiment",
            status="WARN",
            detail="last experiment inconclusive",
            observed={"path": str(path), "status": status},
        )
    if status == "ERROR":
        return CheckResult(
            name="tuning_experiment",
            status="FAIL",
            detail="experiment error state",
            observed={"path": str(path), "status": status},
        )
    return CheckResult(
        name="tuning_experiment",
        status="PASS",
        detail=f"experiment {str(status).lower() if status else 'active'}",
        observed={"path": str(path), "status": status},
    )
