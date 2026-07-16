from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from trading_bot.config.settings import Settings
from trading_bot.learning.experiments.controller import ExperimentController
from trading_bot.learning.experiments.models import MetricSet
from trading_bot.learning.experiments.store import ExperimentStore


def _settings() -> Settings:
    """Default ``Settings`` pointing at non-existent scan/log paths.

    Forces ``propose_tuning_overrides`` to skip its nudges so the test is
    deterministic regardless of any repo-local ``state/scan_results.json``
    or ``logs/strategy_results.jsonl`` from previous runs.
    """
    settings = Settings()
    settings.app.scan_results_path = "/tmp/__nonexistent_scan_results.json"
    settings.app.log_dir = "/tmp/__nonexistent_logs"
    return settings


def _loader():
    """Return ``None``; offline evaluation is stubbed in tests via ``_set_state``."""
    return None


def _seed_overrides(path: Path) -> None:
    """Pre-write overrides so ``propose()`` finds a change vs. default settings.

    ``controller.propose()`` reads ``overrides_path`` to derive the baseline.
    Writing a ``counter_veto_weight`` of 0.5 (vs. settings default of 1.0)
    guarantees ``select_single_change`` returns a 0.25 step toward 1.0.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"supermodel": {"counter_veto_weight": 0.5}}),
        encoding="utf-8",
    )


def test_evaluate_keeps_candidate_when_validation_succeeds(tmp_path: Path) -> None:
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    controller._state = state
    state.status = "CANARY"
    state.baseline_metrics = MetricSet(trades=30, profit_factor=0.6, net_pnl=-200.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=30, profit_factor=0.8, net_pnl=100.0, max_drawdown_pct=12.0)
    state.shadow_metrics = MetricSet(trades=28, profit_factor=0.7, net_pnl=-50.0, max_drawdown_pct=10.0)
    state.canary_closed_trades = 20
    controller._set_state(state)

    final = controller.evaluate()

    assert final is not None
    assert final.status == "KEPT"
    assert overrides.exists()


def test_evaluate_rolls_back_when_candidate_pf_below_early_floor(tmp_path: Path) -> None:
    """Early rollback at 10 trades when PF < 0.50."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    state.status = "CANARY"
    state.canary_closed_trades = 10
    state.baseline_metrics = MetricSet(trades=30, profit_factor=0.6, net_pnl=-200.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=10, profit_factor=0.30, net_pnl=-500.0, max_drawdown_pct=15.0)
    state.shadow_metrics = MetricSet(trades=10, profit_factor=0.7, net_pnl=-50.0, max_drawdown_pct=8.0)
    controller._set_state(state)

    final = controller.evaluate()

    assert final is not None
    assert final.status == "ROLLED_BACK"
    assert final.rolled_back_at is not None
    assert controller.store.load_current() is None
    assert overrides.exists()


def test_evaluate_returns_inconclusive_after_timeout(tmp_path: Path) -> None:
    """Timeout: 10 sessions with <20 trades → INCONCLUSIVE; baseline restored."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    state.status = "CANARY"
    state.canary_closed_trades = 5
    state.market_sessions = [f"2026-07-{i + 1:02d}" for i in range(10)]
    state.baseline_metrics = MetricSet(trades=5, profit_factor=0.6, net_pnl=-100.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=5, profit_factor=0.65, net_pnl=-90.0, max_drawdown_pct=11.0)
    state.shadow_metrics = MetricSet(trades=5, profit_factor=0.6, net_pnl=-100.0, max_drawdown_pct=10.0)
    controller._set_state(state)

    final = controller.evaluate()

    assert final is not None
    assert final.status == "INCONCLUSIVE"
    assert controller.store.load_current() is None
    assert overrides.exists()


def test_evaluate_rolls_back_when_candidate_underperforms_shadow(tmp_path: Path) -> None:
    """Full canary rollback: 20+ trades but PF below shadow + 0.10."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    state.status = "CANARY"
    state.canary_closed_trades = 25
    state.baseline_metrics = MetricSet(trades=30, profit_factor=0.6, net_pnl=-200.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=25, profit_factor=0.65, net_pnl=-150.0, max_drawdown_pct=10.0)
    state.shadow_metrics = MetricSet(trades=25, profit_factor=0.7, net_pnl=-100.0, max_drawdown_pct=10.0)
    controller._set_state(state)

    final = controller.evaluate()

    assert final is not None
    assert final.status == "ROLLED_BACK"
    assert controller.store.load_current() is None
    assert overrides.exists()


def test_evaluate_rolls_back_when_drawdown_exceeds_buffer(tmp_path: Path) -> None:
    """Candidate drawdown > shadow drawdown + 5pp at full canary → ROLLED_BACK."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    state.status = "CANARY"
    state.canary_closed_trades = 25
    state.baseline_metrics = MetricSet(trades=30, profit_factor=0.6, net_pnl=-200.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=25, profit_factor=0.95, net_pnl=200.0, max_drawdown_pct=20.0)
    state.shadow_metrics = MetricSet(trades=25, profit_factor=0.7, net_pnl=-100.0, max_drawdown_pct=10.0)
    controller._set_state(state)

    final = controller.evaluate()

    assert final is not None
    assert final.status == "ROLLED_BACK"


def test_evaluate_rolls_back_when_net_pnl_not_above_shadow(tmp_path: Path) -> None:
    """Candidate net_pnl <= shadow net_pnl at full canary → ROLLED_BACK."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    state.status = "CANARY"
    state.canary_closed_trades = 25
    state.baseline_metrics = MetricSet(trades=30, profit_factor=0.6, net_pnl=-200.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=25, profit_factor=0.95, net_pnl=50.0, max_drawdown_pct=12.0)
    state.shadow_metrics = MetricSet(trades=25, profit_factor=0.7, net_pnl=100.0, max_drawdown_pct=10.0)
    controller._set_state(state)

    final = controller.evaluate()

    assert final is not None
    assert final.status == "ROLLED_BACK"


def test_evaluate_returns_none_when_no_state(tmp_path: Path) -> None:
    """No current experiment → evaluate() returns None without touching store."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    assert controller.evaluate() is None


def test_evaluate_returns_state_unchanged_when_insufficient_evidence(
    tmp_path: Path,
) -> None:
    """Below MIN_CANARY_TRADES, no decision yet → status stays CANARY."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    state.status = "CANARY"
    state.canary_closed_trades = 5
    state.baseline_metrics = MetricSet(trades=5, profit_factor=0.5, net_pnl=-50.0, max_drawdown_pct=5.0)
    state.candidate_metrics = MetricSet(trades=5, profit_factor=0.5, net_pnl=-50.0, max_drawdown_pct=5.0)
    state.shadow_metrics = MetricSet(trades=5, profit_factor=0.5, net_pnl=-50.0, max_drawdown_pct=5.0)
    controller._set_state(state)

    final = controller.evaluate()

    assert final is not None
    assert final.status == "CANARY"
    assert controller.store.load_current() is not None


def test_manual_rollback_restores_baseline(tmp_path: Path) -> None:
    """``rollback()`` clears the active experiment and writes the baseline file."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    state = controller.propose()
    assert state is not None
    state.status = "CANARY"
    state.baseline_metrics = MetricSet(trades=30, profit_factor=0.6, net_pnl=-200.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=30, profit_factor=0.8, net_pnl=100.0, max_drawdown_pct=12.0)
    state.shadow_metrics = MetricSet(trades=28, profit_factor=0.7, net_pnl=-50.0, max_drawdown_pct=10.0)
    state.canary_closed_trades = 20
    controller._set_state(state)

    final = controller.rollback(reason="manual operator request")

    assert final is not None
    assert final.status == "ROLLED_BACK"
    assert final.last_error == "manual operator request"
    assert final.rolled_back_at is not None
    assert controller.store.load_current() is None
    assert overrides.exists()


def test_manual_rollback_returns_none_when_no_active(tmp_path: Path) -> None:
    overrides = tmp_path / "state" / "tuning_overrides.yaml"

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    assert controller.rollback(reason="noop") is None


def test_propose_returns_none_when_experiment_already_active(tmp_path: Path) -> None:
    """``propose()`` refuses to start a second experiment while one is active."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    _seed_overrides(overrides)

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    first = controller.propose()
    assert first is not None

    second = controller.propose()
    assert second is None


def test_propose_returns_none_when_no_change_proposed(tmp_path: Path) -> None:
    """When overrides_path already matches the proposal, no diff → None."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"
    overrides.parent.mkdir(parents=True, exist_ok=True)
    overrides.write_text(
        yaml.safe_dump(
            {
                "supermodel": {
                    "support_threshold": 0.72,
                    "block_threshold": 0.3,
                    "counter_veto_weight": 1.0,
                },
                "strategy_tracker": {
                    "window": 20,
                    "min_win_rate": 0.20,
                    "full_allocation_rate": 0.50,
                },
            }
        ),
        encoding="utf-8",
    )

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    assert controller.propose() is None


def test_status_reports_active_and_inactive(tmp_path: Path) -> None:
    overrides = tmp_path / "state" / "tuning_overrides.yaml"

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=overrides,
    )

    assert controller.status() == {"active": False}

    _seed_overrides(overrides)
    state = controller.propose()
    assert state is not None

    snapshot = controller.status()
    assert snapshot["active"] is True
    assert snapshot["status"] == "PROPOSED"
    assert snapshot["experiment_id"] == state.experiment_id
    assert snapshot["change"] == state.change.model_dump()


def test_run_offline_raises_when_loader_is_none(tmp_path: Path) -> None:
    """``_run_offline`` must raise if no ``StoredBarLoader`` was injected."""
    overrides = tmp_path / "state" / "tuning_overrides.yaml"

    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=None,
        overrides_path=overrides,
    )

    from trading_bot.learning.experiments.models import ParameterChange

    state = ExperimentStore(root=tmp_path / "experiments").load_current()
    fake = type(
        "S",
        (),
        {
            "experiment_id": "x",
            "status": "PROPOSED",
            "change": ParameterChange(
                section="supermodel",
                field="counter_veto_weight",
                baseline=1.0,
                candidate=0.75,
            ),
            "started_at": datetime.now(timezone.utc),
            "canary_closed_trades": 0,
            "market_sessions": [],
            "baseline_metrics": None,
            "candidate_metrics": None,
            "shadow_metrics": None,
            "last_error": None,
            "rolled_back_at": None,
        },
    )()

    import pytest

    with pytest.raises(RuntimeError, match="StoredBarLoader required"):
        controller._run_offline(fake)