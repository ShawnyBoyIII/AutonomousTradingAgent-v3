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
    health_state_dir = state_dir / "burn_in"
    pid_file = health_state_dir / "burn_in.pid"
    heartbeat = health_state_dir / "heartbeat.json"

    results: list[CheckResult] = []
    results.append(check_pid_alive(pid_file))
    results.append(check_heartbeat_fresh(heartbeat, max_age_seconds=90))
    results.append(check_dashboard_health(port=dashboard_port))
    results.append(check_eod_watchdog(pid_file=eod_watchdog_pid_file, now_utc=now))
    results.append(check_open_positions_consistent(db_path=db_path, heartbeat_path=heartbeat))
    results.append(check_market_data_freshness(db_path=db_path, now_utc=now))

    return HealthReport(checks=results, generated_at=now.isoformat())