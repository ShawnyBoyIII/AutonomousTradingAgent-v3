from datetime import datetime, timezone
from pathlib import Path

from trading_bot.config.settings import (
    PaperSettings,
    Settings,
    StrategySettings,
    StrategyTrackerSettings,
    SupermodelSettings,
)
from trading_bot.learning.experiments.controller import ExperimentController
from trading_bot.learning.experiments.models import ExperimentState, ParameterChange
from trading_bot.learning.experiments.store import ExperimentStore


def _settings() -> Settings:
    return Settings(
        paper=PaperSettings(),
        supermodel=SupermodelSettings(),
        strategy_tracker=StrategyTrackerSettings(),
        strategy=StrategySettings(use_v3_signals=True),
    )


def _seed_state(store: ExperimentStore, multiplier: float = 0.5) -> ExperimentState:
    change = ParameterChange(
        section="supermodel",
        field="range_bound_trend_caution_multiplier",
        baseline=1.0,
        candidate=multiplier,
    )
    state = ExperimentState(
        experiment_id="test_exp_001",
        status="CANARY",
        change=change,
        started_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    store.save_current(state)
    return state


def test_terminal_experiment_is_archived_not_deleted(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path)
    _seed_state(store)

    assert store.load_current() is not None

    ExperimentController.archive_terminal(
        store,
        status="ROLLED_BACK",
        reason="manual",
    )

    assert store.load_current() is None
    archived_dir = tmp_path / "archived"
    assert (archived_dir / "test_exp_001").exists()


def test_restore_baseline_handles_original_file_absence(tmp_path: Path) -> None:
    """If the original overrides file did not exist, restore must leave the
    file absent rather than copying an empty baseline.yaml."""
    store = ExperimentStore(tmp_path)
    state = _seed_state(store)
    target = tmp_path / "overrides.yaml"
    assert not target.exists()

    ExperimentController.restore_baseline_to_target(
        store=store,
        experiment_id=state.experiment_id,
        target=target,
    )

    assert not target.exists(), (
        "restore must NOT create target file when the original was absent"
    )


def test_restore_baseline_writes_exact_snapshot_bytes(tmp_path: Path) -> None:
    store = ExperimentStore(tmp_path)
    state = _seed_state(store)
    target = tmp_path / "overrides.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("dirty: true\n", encoding="utf-8")

    exact_bytes = "supermodel:\n  range_bound_trend_caution_multiplier: 0.5\n"
    store.snapshot_overrides(
        experiment_id=state.experiment_id,
        name="baseline",
        overrides={"supermodel": {"range_bound_trend_caution_multiplier": 0.5}},
    )
    snapshot = tmp_path / state.experiment_id / "baseline.yaml"
    snapshot.write_text(exact_bytes, encoding="utf-8")

    ExperimentController.restore_baseline_to_target(
        store=store,
        experiment_id=state.experiment_id,
        target=target,
    )

    assert target.read_text(encoding="utf-8") == exact_bytes


def test_controller_detects_candidate_version_drift(tmp_path: Path) -> None:
    import hashlib

    store = ExperimentStore(tmp_path)
    state = _seed_state(store)
    store.snapshot_overrides(
        experiment_id=state.experiment_id,
        name="candidate",
        overrides={"supermodel": {"range_bound_trend_caution_multiplier": 0.5}},
    )
    snapshot = tmp_path / state.experiment_id / "candidate.yaml"
    expected_checksum = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    # Mutate the snapshot bytes (e.g., operator edits file) and confirm
    # the controller detects drift.
    snapshot.write_text("mutated: true\n", encoding="utf-8")

    drifted = ExperimentController.detect_candidate_drift(
        store,
        state.experiment_id,
        expected_checksum=expected_checksum,
    )
    assert drifted is True

    # No expected_checksum provided AND file exists → no drift detected.
    assert (
        ExperimentController.detect_candidate_drift(store, state.experiment_id)
        is False
    )
