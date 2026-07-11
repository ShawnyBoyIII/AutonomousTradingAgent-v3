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
