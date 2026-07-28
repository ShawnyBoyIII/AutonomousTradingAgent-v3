from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from trading_bot.health.runner import run_health_checks
from trading_bot.health.types import CheckResult


@pytest.fixture
def mock_checks():
    with patch("trading_bot.health.runner.check_pid_alive") as m_pid_alive, \
         patch("trading_bot.health.runner.check_heartbeat_fresh") as m_heartbeat, \
         patch("trading_bot.health.runner.check_dashboard_health") as m_dashboard, \
         patch("trading_bot.health.runner.check_eod_watchdog") as m_eod, \
         patch("trading_bot.health.runner.check_open_positions_consistent") as m_positions, \
         patch("trading_bot.health.runner.check_market_data_freshness") as m_market_data, \
         patch("trading_bot.health.runner.check_tuning_experiment") as m_tuning, \
         patch("trading_bot.health.runner.check_scan_freshness") as m_scan:

        # Setup default mock returns
        m_pid_alive.return_value = CheckResult(name="pid_alive", status="PASS", detail="ok")
        m_heartbeat.return_value = CheckResult(name="heartbeat_fresh", status="PASS", detail="ok")
        m_dashboard.return_value = CheckResult(name="dashboard_health", status="PASS", detail="ok")
        m_eod.return_value = CheckResult(name="eod_watchdog", status="PASS", detail="ok")
        m_positions.return_value = CheckResult(name="open_positions_consistent", status="PASS", detail="ok")
        m_market_data.return_value = CheckResult(name="market_data_freshness", status="PASS", detail="ok")
        m_tuning.return_value = CheckResult(name="tuning_experiment", status="PASS", detail="ok")
        m_scan.return_value = CheckResult(name="scan_freshness", status="PASS", detail="ok")

        yield {
            "pid_alive": m_pid_alive,
            "heartbeat_fresh": m_heartbeat,
            "dashboard_health": m_dashboard,
            "eod_watchdog": m_eod,
            "open_positions_consistent": m_positions,
            "market_data_freshness": m_market_data,
            "tuning_experiment": m_tuning,
            "scan_freshness": m_scan,
        }


def test_runner_happy_path_default_args(mock_checks):
    state_dir = Path("/mock/state")
    db_path = Path("/mock/db/trading.db")
    dashboard_port = 8080
    eod_watchdog_pid_file = Path("/mock/eod.pid")

    report = run_health_checks(
        state_dir=state_dir,
        db_path=db_path,
        dashboard_port=dashboard_port,
        eod_watchdog_pid_file=eod_watchdog_pid_file,
    )

    assert len(report.checks) == 7
    assert all(c.status == "PASS" for c in report.checks)
    assert datetime.fromisoformat(report.generated_at)

    health_state_dir = state_dir / "burn_in"
    pid_file = health_state_dir / "burn_in.pid"
    heartbeat = health_state_dir / "heartbeat.json"
    expected_cache_db = db_path.parent / "market_data_cache.db"

    mock_checks["pid_alive"].assert_called_once_with(pid_file)
    mock_checks["heartbeat_fresh"].assert_called_once_with(heartbeat, max_age_seconds=90)
    mock_checks["dashboard_health"].assert_called_once_with(port=dashboard_port)

    call_kwargs_eod = mock_checks["eod_watchdog"].call_args.kwargs
    assert call_kwargs_eod["pid_file"] == eod_watchdog_pid_file
    generated_now = call_kwargs_eod["now_utc"]

    mock_checks["open_positions_consistent"].assert_called_once_with(db_path=db_path, heartbeat_path=heartbeat)
    mock_checks["market_data_freshness"].assert_called_once_with(db_path=db_path, cache_db_path=expected_cache_db, now_utc=generated_now)
    mock_checks["tuning_experiment"].assert_called_once_with(state_dir=state_dir, now_utc=generated_now)
    mock_checks["scan_freshness"].assert_not_called()


def test_runner_all_optional_args(mock_checks):
    state_dir = Path("/mock/state")
    db_path = Path("/mock/db/trading.db")
    dashboard_port = 8080
    eod_watchdog_pid_file = Path("/mock/eod.pid")

    scan_results_path = Path("/mock/scan.json")
    cache_db_path = Path("/mock/custom_cache.db")
    now_utc = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)

    report = run_health_checks(
        state_dir=state_dir,
        db_path=db_path,
        dashboard_port=dashboard_port,
        eod_watchdog_pid_file=eod_watchdog_pid_file,
        scan_results_path=scan_results_path,
        cache_db_path=cache_db_path,
        now_utc=now_utc,
    )

    assert len(report.checks) == 8
    assert report.generated_at == now_utc.isoformat()

    mock_checks["market_data_freshness"].assert_called_once_with(db_path=db_path, cache_db_path=cache_db_path, now_utc=now_utc)
    mock_checks["tuning_experiment"].assert_called_once_with(state_dir=state_dir, now_utc=now_utc)
    mock_checks["scan_freshness"].assert_called_once_with(scan_results_path, now_utc=now_utc)
    mock_checks["eod_watchdog"].assert_called_once_with(pid_file=eod_watchdog_pid_file, now_utc=now_utc)


def test_runner_mixed_results(mock_checks):
    state_dir = Path("/mock/state")
    db_path = Path("/mock/db/trading.db")
    dashboard_port = 8080
    eod_watchdog_pid_file = Path("/mock/eod.pid")

    mock_checks["pid_alive"].return_value = CheckResult(name="pid_alive", status="FAIL", detail="pid not found")
    mock_checks["heartbeat_fresh"].return_value = CheckResult(name="heartbeat_fresh", status="WARN", detail="stale")

    report = run_health_checks(
        state_dir=state_dir,
        db_path=db_path,
        dashboard_port=dashboard_port,
        eod_watchdog_pid_file=eod_watchdog_pid_file,
    )

    assert len(report.checks) == 7

    statuses = [c.status for c in report.checks]
    assert statuses.count("PASS") == 5
    assert statuses.count("WARN") == 1
    assert statuses.count("FAIL") == 1
    assert report.worst_status() == "FAIL"
