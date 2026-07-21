from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from trading_bot.learning.experiments.proposal import select_single_change
from trading_bot.learning.tuning_overrides import (
    TREND_REDUCER_FIRST_STEP,
    _maybe_apply_trend_reducer,
    propose_tuning_overrides,
)


def test_select_single_change_picks_counter_veto_weight_first() -> None:
    baseline = {
        "supermodel": {"counter_veto_weight": 1.0, "block_threshold": 0.3},
        "strategy_tracker": {"full_allocation_rate": 0.5},
    }
    proposed = {
        "supermodel": {"counter_veto_weight": 0.5, "block_threshold": 0.2},
        "strategy_tracker": {"full_allocation_rate": 0.6},
    }

    change = select_single_change(baseline, proposed)

    assert change is not None
    assert (change.section, change.field) == ("supermodel", "counter_veto_weight")
    assert change.baseline == 1.0
    assert change.candidate == 0.75


def test_select_single_change_ignores_non_allowlisted_fields() -> None:
    baseline = {"risk": {"max_shares_per_position": 50}}
    proposed = {"risk": {"max_shares_per_position": 75}}

    assert select_single_change(baseline, proposed) is None


def test_select_single_change_returns_none_when_no_diff() -> None:
    baseline = {"supermodel": {"counter_veto_weight": 1.0}}
    proposed = {"supermodel": {"counter_veto_weight": 1.0}}

    assert select_single_change(baseline, proposed) is None


def test_select_single_change_includes_range_bound_trend_multiplier() -> None:
    """The new entry-policy multiplier must be in the proposal allowlist so
    nightly tuning can offer the candidate and offline replay can test it."""
    baseline = {
        "supermodel": {
            "counter_veto_weight": 1.0,
            "block_threshold": 0.3,
            "support_threshold": 0.7,
            "range_bound_trend_caution_multiplier": 1.0,
        }
    }
    proposed = {
        "supermodel": {
            "counter_veto_weight": 1.0,
            "block_threshold": 0.3,
            "support_threshold": 0.7,
            "range_bound_trend_caution_multiplier": 0.5,
        }
    }

    change = select_single_change(baseline, proposed)

    assert change is not None
    assert (change.section, change.field) == (
        "supermodel",
        "range_bound_trend_caution_multiplier",
    )
    assert change.baseline == 1.0
    # First-step reducer: discrete 1.0 -> 0.5 (allowlist step = 0.5)
    assert change.candidate == 0.5


# -----------------------------------------------------------------------------
# Reducer unit tests: the trend reducer block in propose_tuning_overrides must
# only propose a reduction when the cohort evidence shows the targeted bucket
# is losing. The reducer never raises the multiplier; it only surfaces a
# discrete first-step reduction candidate.
# -----------------------------------------------------------------------------


class _StubSupermodel:
    support_threshold = 0.72
    block_threshold = 0.25
    counter_veto_weight = 0.75
    range_bound_trend_caution_multiplier = 1.0


class _StubStrategyTracker:
    window = 20
    min_win_rate = 0.0
    full_allocation_rate = 0.2


class _StubApp:
    state_db_path = "/tmp/_does_not_exist.db"
    log_dir = "/tmp"
    scan_results_path = ""
    timezone = "UTC"


class _StubSettings:
    supermodel = _StubSupermodel()
    strategy_tracker = _StubStrategyTracker()
    app = _StubApp()
    eod_data_store = None


def _seed_trend_sells(db_path: Path, *, n_sells: int, n_wins: int, avg_pnl: float) -> None:
    """Populate ``orders`` with ``n_sells`` trend-following SELL rows.

    Each row's pnl is computed so that ``n_wins`` rows are positive and
    the cohort average matches ``avg_pnl``. Per-row magnitudes are tuned
    to satisfy the aggregate using a small flat-loss pool + a single
    larger win.
    """
    import sqlite3

    if n_wins > n_sells:
        raise ValueError("n_wins cannot exceed n_sells")
    n_losses = n_sells - n_wins
    if n_losses == 0:
        win_per = avg_pnl
    else:
        # Solve avg = (win_per*n_wins + loss_per*n_losses) / n_sells.
        win_per = avg_pnl + 1.0
        loss_per = (avg_pnl * n_sells - win_per * n_wins) / n_losses
    now = datetime.now(timezone.utc).isoformat()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                ticker TEXT,
                side TEXT,
                quantity INTEGER,
                fill_price REAL,
                fees REAL,
                filled_at TEXT,
                pnl REAL,
                strategy_tag TEXT DEFAULT ''
            )
            """
        )
        # Avoid colliding with previously seeded rows in the same test run.
        conn.execute("DELETE FROM orders WHERE strategy_tag = 'v3-trend_following'")
        for i in range(n_sells):
            pnl = win_per if i < n_wins else loss_per
            conn.execute(
                """
                INSERT INTO orders
                  (id, ticker, side, quantity, fill_price, fees,
                   filled_at, pnl, strategy_tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"trend-{i}",
                    "SPY",
                    "SELL",
                    1,
                    100.0,
                    0.0,
                    now,
                    float(pnl),
                    "v3-trend_following",
                ),
            )
        conn.commit()


def _settings_with_db(db_path: Path) -> _StubSettings:
    settings = _StubSettings()
    settings.supermodel = _StubSupermodel()
    settings.app = _StubApp()
    settings.app.state_db_path = str(db_path)
    return settings


def test_trend_reducer_no_op_when_no_db() -> None:
    """Missing state DB must not break the proposal path."""
    settings = _StubSettings()
    settings.app.state_db_path = "/tmp/does_not_exist_reducer.db"
    proposal: dict[str, dict[str, float | int]] = {
        "supermodel": {"range_bound_trend_caution_multiplier": 1.0},
    }
    _maybe_apply_trend_reducer(settings, proposal)
    assert proposal["supermodel"]["range_bound_trend_caution_multiplier"] == 1.0


def test_trend_reducer_no_op_when_insufficient_trades(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_trend_sells(db, n_sells=5, n_wins=2, avg_pnl=-10.0)
    settings = _settings_with_db(db)
    proposal: dict[str, dict[str, float | int]] = {
        "supermodel": {"range_bound_trend_caution_multiplier": 1.0},
    }
    _maybe_apply_trend_reducer(settings, proposal)
    assert proposal["supermodel"]["range_bound_trend_caution_multiplier"] == 1.0


def test_trend_reducer_no_op_when_winning(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_trend_sells(db, n_sells=20, n_wins=15, avg_pnl=5.0)
    settings = _settings_with_db(db)
    proposal: dict[str, dict[str, float | int]] = {
        "supermodel": {"range_bound_trend_caution_multiplier": 1.0},
    }
    _maybe_apply_trend_reducer(settings, proposal)
    assert proposal["supermodel"]["range_bound_trend_caution_multiplier"] == 1.0


def test_trend_reducer_proposes_reduction_on_losing_cohort(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_trend_sells(db, n_sells=20, n_wins=8, avg_pnl=-12.0)
    settings = _settings_with_db(db)
    proposal: dict[str, dict[str, float | int]] = {
        "supermodel": {"range_bound_trend_caution_multiplier": 1.0},
    }
    _maybe_apply_trend_reducer(settings, proposal)
    assert (
        proposal["supermodel"]["range_bound_trend_caution_multiplier"]
        == TREND_REDUCER_FIRST_STEP
    )


def test_trend_reducer_no_op_when_already_below_first_step(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_trend_sells(db, n_sells=20, n_wins=8, avg_pnl=-12.0)
    settings = _settings_with_db(db)
    settings.supermodel.range_bound_trend_caution_multiplier = 0.5
    proposal: dict[str, dict[str, float | int]] = {
        "supermodel": {"range_bound_trend_caution_multiplier": 0.5},
    }
    _maybe_apply_trend_reducer(settings, proposal)
    # Must not propose a further reduction in a single step.
    assert proposal["supermodel"]["range_bound_trend_caution_multiplier"] == 0.5


def test_trend_reducer_ignores_stale_evidence(tmp_path: Path) -> None:
    """Evidence older than the lookback window must not trigger a reduction."""
    db = tmp_path / "state.db"
    _seed_trend_sells(db, n_sells=20, n_wins=8, avg_pnl=-12.0)
    # Backdate the seeded rows so they fall outside the lookback.
    import sqlite3

    old = (
        datetime.now(timezone.utc) - timedelta(hours=72)
    ).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE orders SET filled_at = ? WHERE strategy_tag = 'v3-trend_following'",
            (old,),
        )
        conn.commit()

    settings = _settings_with_db(db)
    proposal: dict[str, dict[str, float | int]] = {
        "supermodel": {"range_bound_trend_caution_multiplier": 1.0},
    }
    _maybe_apply_trend_reducer(settings, proposal)
    assert proposal["supermodel"]["range_bound_trend_caution_multiplier"] == 1.0


def test_propose_tuning_overrides_emits_trend_reduction(
    monkeypatch, tmp_path: Path
) -> None:
    """End-to-end: a losing cohort must make propose_tuning_overrides emit a
    full proposal with the multiplier reduction visible."""
    db = tmp_path / "state.db"
    _seed_trend_sells(db, n_sells=20, n_wins=8, avg_pnl=-12.0)
    settings = _settings_with_db(db)

    # Avoid touching any real log directory or EOD data store.
    import trading_bot.strategy.strategy_tracker as tracker_mod
    import trading_bot.learning.tuning_overrides as overrides_mod
    monkeypatch.setattr(tracker_mod, "strategy_summary", lambda log_dir, window: [])
    monkeypatch.setattr(
        overrides_mod, "_maybe_nudge_window_from_data_store", lambda *a, **kw: None
    )

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    proposal = propose_tuning_overrides(log_dir, settings, None)

    assert proposal["supermodel"]["range_bound_trend_caution_multiplier"] == 0.5
    # Other tunable fields should remain at their current values.
    assert proposal["supermodel"]["counter_veto_weight"] == 0.75

    # And select_single_change should now surface the trend change. The
    # baseline must mirror what the controller reads from the live
    # overrides file (which always carries the multiplier key).
    baseline_file = yaml.safe_load(
        """
        supermodel:
          support_threshold: 0.72
          block_threshold: 0.25
          counter_veto_weight: 0.75
          range_bound_trend_caution_multiplier: 1.0
        strategy_tracker:
          window: 20
          min_win_rate: 0.0
          full_allocation_rate: 0.2
        """
    )
    change = select_single_change(baseline_file, proposal)
    assert change is not None
    assert (change.section, change.field) == (
        "supermodel",
        "range_bound_trend_caution_multiplier",
    )
    # First-step reducer is a discrete reduction, not a fractional step.
    assert change.baseline == 1.0
    assert change.candidate == TREND_REDUCER_FIRST_STEP
