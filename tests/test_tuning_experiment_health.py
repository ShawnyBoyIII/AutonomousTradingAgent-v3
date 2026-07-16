"""Health-check coverage for the tuning-experiment state file."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from trading_bot.health.checks import check_tuning_experiment


def _write_current(state_dir: Path, status: str | None) -> Path:
    exp_dir = state_dir / "tuning_experiments"
    exp_dir.mkdir(parents=True, exist_ok=True)
    path = exp_dir / "current.json"
    if status is None:
        return path
    payload = {
        "experiment_id": "x",
        "status": status,
        "change": {
            "section": "supermodel",
            "field": "counter_veto_weight",
            "baseline": 0.5,
            "candidate": 1.0,
        },
        "started_at": "2026-07-15T00:00:00+00:00",
        "canary_closed_trades": 0,
        "market_sessions": [],
        "baseline_metrics": None,
        "candidate_metrics": None,
        "shadow_metrics": None,
        "last_error": None,
        "rolled_back_at": None,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_check_tuning_experiment_pass_when_no_state(tmp_path: Path) -> None:
    """No current.json → PASS with a "no active experiment" detail."""
    result = check_tuning_experiment(tmp_path, datetime.now(timezone.utc))
    assert result.name == "tuning_experiment"
    assert result.status == "PASS"
    assert "no active experiment" in result.detail.lower()


def test_check_tuning_experiment_pass_when_status_kept(tmp_path: Path) -> None:
    """A successful KEPT experiment is reported as PASS."""
    _write_current(tmp_path, "KEPT")
    result = check_tuning_experiment(tmp_path, datetime.now(timezone.utc))
    assert result.status == "PASS"
    assert "kept" in result.detail.lower()


def test_check_tuning_experiment_warn_when_inconclusive(tmp_path: Path) -> None:
    """An INCONCLUSIVE experiment is a soft-warning, not a hard fail."""
    _write_current(tmp_path, "INCONCLUSIVE")
    result = check_tuning_experiment(tmp_path, datetime.now(timezone.utc))
    assert result.status == "WARN"
    assert "inconclusive" in result.detail.lower()


def test_check_tuning_experiment_fail_when_status_error(tmp_path: Path) -> None:
    """An ERROR experiment is a hard fail so the burn-in loop halts."""
    _write_current(tmp_path, "ERROR")
    result = check_tuning_experiment(tmp_path, datetime.now(timezone.utc))
    assert result.status == "FAIL"
    assert "error" in result.detail.lower()


def test_check_tuning_experiment_fail_when_state_corrupt(tmp_path: Path) -> None:
    """Corrupt JSON on disk surfaces as FAIL with a parseable detail."""
    exp_dir = tmp_path / "tuning_experiments"
    exp_dir.mkdir(parents=True)
    (exp_dir / "current.json").write_text("{not valid json", encoding="utf-8")
    result = check_tuning_experiment(tmp_path, datetime.now(timezone.utc))
    assert result.status == "FAIL"
    assert "corrupt" in result.detail.lower() or "state" in result.detail.lower()