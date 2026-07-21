import json
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.config.settings import AppSettings, Settings
from trading_bot.health.checks import (
    check_dashboard_health,
    check_scan_freshness,
)
from trading_bot.health.runner import run_health_checks


def _write_scan(path: Path, when: datetime, deadline_exceeded: bool = False) -> None:
    payload = {
        "mode": "scan",
        "generated_at": when.isoformat(),
        "summary": {
            "symbols": 5,
            "approved": 2,
            "errors": 1,
            "deadline_exceeded": deadline_exceeded,
            "elapsed_seconds": 12.3,
        },
        "candidates": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_scan_freshness_passes_when_recent(tmp_path: Path) -> None:
    scan_path = tmp_path / "scan.json"
    now = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)
    _write_scan(scan_path, when=now)

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "PASS"


def test_scan_freshness_warns_when_stale(tmp_path: Path) -> None:
    scan_path = tmp_path / "scan.json"
    now = datetime(2026, 7, 18, 14, 5, tzinfo=timezone.utc)
    _write_scan(scan_path, when=datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc))

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "WARN"


def test_scan_freshness_fails_when_missing(tmp_path: Path) -> None:
    scan_path = tmp_path / "missing.json"
    now = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "FAIL"
    assert "missing" in result.detail.lower()


def test_scan_freshness_flags_deadline_exceeded(tmp_path: Path) -> None:
    scan_path = tmp_path / "scan.json"
    now = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)
    _write_scan(scan_path, when=now, deadline_exceeded=True)

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "WARN"
    assert "deadline" in result.detail.lower()


def test_scan_freshness_does_not_crash_on_non_object_payload(tmp_path: Path) -> None:
    scan_path = tmp_path / "scan.json"
    scan_path.write_text("[]", encoding="utf-8")
    now = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "FAIL"
    assert "malformed" in result.detail.lower()


def test_scan_freshness_does_not_crash_on_non_dict_summary(tmp_path: Path) -> None:
    scan_path = tmp_path / "scan.json"
    now = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)
    scan_path.write_text(
        json.dumps({"mode": "scan", "generated_at": now.isoformat(), "summary": []}),
        encoding="utf-8",
    )

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "PASS"


def test_scan_freshness_does_not_crash_on_missing_generated_at(tmp_path: Path) -> None:
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps({"mode": "scan", "summary": {}}), encoding="utf-8")
    now = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "FAIL"


def test_scan_freshness_does_not_crash_on_invalid_generated_at(tmp_path: Path) -> None:
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(
        json.dumps({"mode": "scan", "generated_at": "not-a-date"}),
        encoding="utf-8",
    )
    now = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)

    result = check_scan_freshness(
        scan_results_path=scan_path,
        now_utc=now,
        max_age_seconds=120,
    )

    assert result.status == "FAIL"


def test_dashboard_health_probe_fails_on_url_error(monkeypatch) -> None:
    """`check_dashboard_health` must surface a FAIL when urlopen raises URLError.

    Network isolation: monkeypatch urlopen so the test never actually
    opens a loopback socket. Result must depend only on the patched
    response, not on local port occupancy.
    """
    from urllib.error import URLError

    def fake_urlopen(request, timeout=1.0):
        raise URLError("connection refused")

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen", fake_urlopen
    )
    result = check_dashboard_health(port=65500)
    assert result.status == "FAIL"
    assert "unreachable" in result.detail


def test_dashboard_health_probe_passes_on_200(monkeypatch) -> None:
    """A 200 response from urlopen should produce a PASS.

    Network isolation: monkeypatch urlopen so the test never opens a
    real socket. Verifies that the check is purely a function of the
    mocked HTTP response.
    """
    class _FakeResponse:
        status = 200

    def fake_urlopen(request, timeout=1.0):
        return _FakeResponse()

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen", fake_urlopen
    )
    result = check_dashboard_health(port=8000)
    assert result.status == "PASS"


def test_dashboard_health_probe_warns_on_non_200(monkeypatch) -> None:
    """A non-200 response from urlopen should produce a WARN (not a PASS).

    Network isolation: monkeypatch urlopen so the test never opens a
    real socket. Verifies that the check correctly distinguishes
    partial failure (non-200) from outright unreachability (URLError).
    """
    class _FakeResponse:
        status = 503

    def fake_urlopen(request, timeout=1.0):
        return _FakeResponse()

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen", fake_urlopen
    )
    result = check_dashboard_health(port=8000)
    assert result.status == "WARN"
    assert result.observed["status_code"] == 503


def test_health_runner_includes_scan_freshness(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        app=AppSettings(
            state_db_path=str(tmp_path / "state.db"),
            log_dir=str(tmp_path / "logs"),
            dashboard_summary_path=str(tmp_path / "dashboard.json"),
            scan_results_path=str(tmp_path / "scan.json"),
            portfolio_summary_path=str(tmp_path / "portfolio.json"),
            backtest_summary_path=str(tmp_path / "backtest.json"),
        )
    )
    settings.app.dashboard_port = 65501  # arbitrary port; we mock the probe

    # Network isolation: monkeypatch urlopen so the runner's dashboard probe
    # never opens a real socket. Test result depends only on the patched
    # response, not on local port occupancy.
    from urllib.error import URLError

    monkeypatch.setattr(
        "trading_bot.health.checks.urlopen",
        lambda request, timeout=1.0: (_ for _ in ()).throw(URLError("refused")),
    )

    report = run_health_checks(
        state_dir=tmp_path,
        db_path=tmp_path / "state.db",
        dashboard_port=settings.app.dashboard_port,
        eod_watchdog_pid_file=tmp_path / "watchdog.pid",
        scan_results_path=tmp_path / "scan.json",
        now_utc=datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc),
    )

    names = {check.name for check in report.checks}
    assert "scan_freshness" in names
    assert "dashboard_health" in names


def test_app_settings_dashboard_port_default() -> None:
    settings = Settings()
    assert settings.app.dashboard_port == 8000


def test_app_settings_dashboard_port_override(monkeypatch) -> None:
    monkeypatch.setenv("DASHBOARD_PORT", "9123")
    from trading_bot.config.loader import _load_env_overrides

    settings = Settings()
    _load_env_overrides(settings)
    assert settings.app.dashboard_port == 9123
