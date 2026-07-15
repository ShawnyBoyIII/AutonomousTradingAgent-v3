from __future__ import annotations

import json
from pathlib import Path

import yaml

from trading_bot.learning.experiments.models import (
    ExperimentState,
    MetricSet,
    ParameterChange,
)
from trading_bot.learning.experiments.store import ExperimentStore


def test_experiment_store_atomic_round_trip(tmp_path: Path) -> None:
    store = ExperimentStore(root=tmp_path / "experiments")
    state = ExperimentState(
        experiment_id="2026-07-14T13:42:17Z__counter_veto_weight-1.00-to-0.75",
        status="PROPOSED",
        change=ParameterChange(
            section="supermodel",
            field="counter_veto_weight",
            baseline=1.0,
            candidate=0.75,
        ),
        started_at="2026-07-14T13:42:17+00:00",
        baseline_metrics=MetricSet(
            trades=200, profit_factor=0.74, net_pnl=-533.47, max_drawdown_pct=44.93
        ),
    )

    store.save_current(state)
    loaded = store.load_current()

    assert loaded == state
    assert (tmp_path / "experiments" / "current.json").exists()


def test_experiment_store_append_event(tmp_path: Path) -> None:
    store = ExperimentStore(root=tmp_path / "experiments")
    store.append_event({"event": "proposed", "experiment_id": "abc"})
    store.append_event({"event": "offline_rejected", "experiment_id": "abc"})
    lines = (tmp_path / "experiments" / "events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["event"] == "proposed"
    assert json.loads(lines[1])["event"] == "offline_rejected"


def test_experiment_store_restore_baseline_writes_exact_bytes(tmp_path: Path) -> None:
    store = ExperimentStore(root=tmp_path / "experiments")
    overrides = {"supermodel": {"counter_veto_weight": 1.0}}
    store.snapshot_overrides("abc", "baseline", overrides)
    target = tmp_path / "state" / "tuning_overrides.yaml"
    assert store.restore_baseline("abc", target) is True
    assert target.read_text(encoding="utf-8").strip() == yaml.safe_dump(
        overrides, sort_keys=False
    ).strip()