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
