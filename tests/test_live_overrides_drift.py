"""Live overrides drift detection at activation and rollback.

Bug: the controller overwrote the live tuning_overrides.yaml without
verifying whether an operator had hand-edited it since the experiment
was proposed. An operator's edit could be silently replaced by either
the candidate (at activation) or the original baseline (at rollback).

Fix: store.detect_baseline_drift compares the live file's checksum to
the baseline checksum recorded at proposal time and returns True when
they differ (or when file presence state diverged). The controller logs
a ``baseline_drift_detected`` event at both activation and rollback but
does NOT refuse to proceed — operator edits are the operator's call;
we just need to record them for audit.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from trading_bot.learning.experiments.models import ParameterChange


def _setup(tmp_path):
    """Create store + state with a recorded baseline checksum."""
    from trading_bot.config.settings import Settings
    from trading_bot.learning.experiments.controller import ExperimentController
    from trading_bot.learning.experiments.store import ExperimentStore

    settings = Settings(
        app={
            "state_db_path": str(tmp_path / "state.db"),
            "log_dir": str(tmp_path),
        }
    )
    store = ExperimentStore(tmp_path / "tuning_experiments")
    overrides = tmp_path / "tuning_overrides.yaml"
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 1.0\n",
        encoding="utf-8",
    )
    return settings, store, overrides, ExperimentController(
        store=store,
        overrides_path=overrides,
        settings=settings,
        bar_loader=None,
    )


def _make_change() -> ParameterChange:
    return ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=0.5,
    )


def _events(tmp_path) -> list[dict]:
    events_path = tmp_path / "tuning_experiments" / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_detect_baseline_drift_returns_false_when_unchanged(tmp_path):
    settings, store, overrides, _ = _setup(tmp_path)
    from trading_bot.learning.experiments.models import ExperimentState

    state = ExperimentState(
        experiment_id="exp-1",
        status="PROPOSED",
        change=_make_change(),
        started_at=datetime.now(timezone.utc),
        baseline_checksum=store.checksum(overrides),
        baseline_was_absent=False,
    )
    store.save_current(state)

    assert store.detect_baseline_drift(
        overrides, state.baseline_checksum, state.baseline_was_absent
    ) is False


def test_detect_baseline_drift_returns_true_when_edited(tmp_path):
    settings, store, overrides, _ = _setup(tmp_path)
    from trading_bot.learning.experiments.models import ExperimentState

    state = ExperimentState(
        experiment_id="exp-1",
        status="PROPOSED",
        change=_make_change(),
        started_at=datetime.now(timezone.utc),
        baseline_checksum=store.checksum(overrides),
        baseline_was_absent=False,
    )
    store.save_current(state)

    # Operator hand-edits the live overrides
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 0.7\n",
        encoding="utf-8",
    )

    assert store.detect_baseline_drift(
        overrides, state.baseline_checksum, state.baseline_was_absent
    ) is True


def test_detect_baseline_drift_handles_absent_baseline(tmp_path):
    settings, store, overrides, _ = _setup(tmp_path)
    from trading_bot.learning.experiments.models import ExperimentState

    state = ExperimentState(
        experiment_id="exp-1",
        status="PROPOSED",
        change=_make_change(),
        started_at=datetime.now(timezone.utc),
        baseline_checksum="",
        baseline_was_absent=True,
    )
    store.save_current(state)

    # Live file did not exist at proposal; if operator has now created one,
    # that's drift.
    overrides.write_text("supermodel:\n  counter_veto_weight: 0.5\n", encoding="utf-8")
    assert store.detect_baseline_drift(
        overrides, state.baseline_checksum, state.baseline_was_absent
    ) is True

    overrides.unlink()
    assert store.detect_baseline_drift(
        overrides, state.baseline_checksum, state.baseline_was_absent
    ) is False


def test_detect_baseline_drift_flags_deleted_file(tmp_path):
    settings, store, overrides, _ = _setup(tmp_path)
    from trading_bot.learning.experiments.models import ExperimentState

    state = ExperimentState(
        experiment_id="exp-1",
        status="PROPOSED",
        change=_make_change(),
        started_at=datetime.now(timezone.utc),
        baseline_checksum=store.checksum(overrides),
        baseline_was_absent=False,
    )
    store.save_current(state)

    overrides.unlink()
    assert store.detect_baseline_drift(
        overrides, state.baseline_checksum, state.baseline_was_absent
    ) is True


def test_rollback_logs_drift_event(tmp_path):
    """rollback() must log baseline_drift_detected when the live file drifted."""
    settings, store, overrides, controller = _setup(tmp_path)
    from trading_bot.learning.experiments.models import ExperimentState

    state = ExperimentState(
        experiment_id="exp-1",
        status="CANARY",
        change=_make_change(),
        started_at=datetime.now(timezone.utc),
        baseline_checksum=store.checksum(overrides),
        baseline_was_absent=False,
    )
    # Seed baseline snapshot so finalize_terminal can restore on rollback.
    store.snapshot_overrides_bytes(
        state.experiment_id, "baseline", overrides.read_bytes()
    )
    store.save_current(state)

    # Operator edits after propose
    overrides.write_text(
        "supermodel:\n  range_bound_trend_caution_multiplier: 0.7\n",
        encoding="utf-8",
    )

    controller.rollback(reason="manual operator override")

    events = _events(tmp_path)
    drift_events = [e for e in events if e.get("event") == "baseline_drift_detected"]
    assert len(drift_events) == 1
    assert drift_events[0]["phase"] == "rollback"
    assert drift_events[0]["reason"] == "manual operator override"


def test_rollback_no_drift_event_when_unchanged(tmp_path):
    """No drift event when live file matches baseline_checksum."""
    settings, store, overrides, controller = _setup(tmp_path)
    from trading_bot.learning.experiments.models import ExperimentState

    state = ExperimentState(
        experiment_id="exp-1",
        status="CANARY",
        change=_make_change(),
        started_at=datetime.now(timezone.utc),
        baseline_checksum=store.checksum(overrides),
        baseline_was_absent=False,
    )
    # Seed baseline snapshot so finalize_terminal can restore on rollback.
    store.snapshot_overrides_bytes(
        state.experiment_id, "baseline", overrides.read_bytes()
    )
    store.save_current(state)

    controller.rollback(reason="canary failed")

    events = _events(tmp_path)
    drift_events = [e for e in events if e.get("event") == "baseline_drift_detected"]
    assert drift_events == []