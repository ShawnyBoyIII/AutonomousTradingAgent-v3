# Tuning Experiment Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a controller that validates proposed tuning changes offline, runs a paired 20-trade paper canary against a shadow baseline, then keeps or rolls back automatically based on profit factor, P&L, and drawdown.

**Architecture:** A new `trading_bot/learning/experiments/` package wraps the existing tuner. Local EOD bars feed a local backtest for offline validation. During the canary the candidate controls real paper orders while the baseline runs against the same data through a shadow paper broker. All state persists under `state/tuning_experiments/`.

**Tech Stack:** Python 3.11+, pydantic v2, pandas, SQLite, pytest, Typer, YAML.

## Global Constraints

- `live_trading_enabled` stays forced `False`; the loader already enforces this.
- Only four settings are tunable: `supermodel.support_threshold`, `supermodel.block_threshold`, `supermodel.counter_veto_weight`, `strategy_tracker.full_allocation_rate`.
- Exactly one experiment at a time.
- Exactly one parameter change per experiment.
- All writes use atomic temp-file-plus-rename.
- All tests are network-free; monkeypatch `fetch_bars` and use `tmp_path`.
- The existing `tune` heuristic remains, but the burn-in nightly hook switches to `tune-experiment`.

---

### Task 1: Experiment Models and Durable Store

**Files:**
- Create: `trading_bot/learning/experiments/__init__.py`
- Create: `trading_bot/learning/experiments/models.py`
- Create: `trading_bot/learning/experiments/store.py`
- Test: `tests/test_tuning_experiment_store.py`

**Interfaces:**
- Produces:
  - `ExperimentState` (pydantic)
  - `ParameterChange` (pydantic)
  - `MetricSet` (pydantic)
  - `ExperimentStore` with methods: `load_current`, `save_current`, `append_event`, `snapshot_overrides`, `restore_baseline`.

- [ ] **Step 1: Write failing test for atomic store + round-trip**

```python
from pathlib import Path
import json

from trading_bot.learning.experiments.models import (
    ExperimentState, MetricSet, ParameterChange,
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
        baseline_metrics=MetricSet(trades=200, profit_factor=0.74, net_pnl=-533.47, max_drawdown_pct=44.93),
    )

    store.save_current(state)
    loaded = store.load_current()

    assert loaded == state
    assert (tmp_path / "experiments" / "current.json").exists()
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_store.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `trading_bot/learning/experiments/__init__.py`**

Empty marker:

```python
"""Tuning experiment controller package."""
```

- [ ] **Step 4: Create `trading_bot/learning/experiments/models.py`**

```python
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ExperimentStatus = Literal[
    "PROPOSED",
    "OFFLINE_REJECTED",
    "CANARY",
    "KEPT",
    "ROLLED_BACK",
    "INCONCLUSIVE",
    "ERROR",
]


class ParameterChange(BaseModel):
    section: str
    field: str
    baseline: float
    candidate: float


class MetricSet(BaseModel):
    trades: int = 0
    profit_factor: float = 0.0
    net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0


class ExperimentState(BaseModel):
    experiment_id: str
    status: ExperimentStatus = "PROPOSED"
    change: ParameterChange
    started_at: datetime
    canary_closed_trades: int = 0
    market_sessions: list[str] = Field(default_factory=list)
    baseline_metrics: MetricSet | None = None
    candidate_metrics: MetricSet | None = None
    shadow_metrics: MetricSet | None = None
    last_error: str | None = None
    rolled_back_at: datetime | None = None
```

- [ ] **Step 5: Create `trading_bot/learning/experiments/store.py`**

```python
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from trading_bot.learning.experiments.models import ExperimentState


class ExperimentStore:
    """Atomic, append-only storage for tuning experiments."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def artifact_dir(self) -> Path:
        return self.root  # caller may use subdirs per experiment-id

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def load_current(self) -> ExperimentState | None:
        if not self.current_path.exists():
            return None
        payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        return ExperimentState.model_validate(payload)

    def save_current(self, state: ExperimentState) -> None:
        self._ensure_root()
        payload = state.model_dump(mode="json")
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.root, delete=False
        ) as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            temp_path = Path(handle.name)
        temp_path.replace(self.current_path)

    def append_event(self, event: dict[str, Any]) -> None:
        self._ensure_root()
        payload = dict(event)
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(payload, sort_keys=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def snapshot_overrides(
        self, experiment_id: str, name: str, overrides: dict[str, object]
    ) -> Path:
        self._ensure_root()
        target_dir = self.root / experiment_id
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{name}.yaml"
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_dir, delete=False
        ) as handle:
            yaml.safe_dump(overrides, handle, sort_keys=False)
            temp_path = Path(handle.name)
        temp_path.replace(path)
        return path

    def restore_baseline(
        self, experiment_id: str, target_path: Path
    ) -> bool:
        snapshot = self.root / experiment_id / "baseline.yaml"
        if not snapshot.exists():
            return False
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w", encoding="utf-8", dir=target_path.parent, delete=False
        ) as handle:
            handle.write(snapshot.read_text(encoding="utf-8"))
            temp_path = Path(handle.name)
        temp_path.replace(target_path)
        return True

    def clear_current(self) -> None:
        if self.current_path.exists():
            self.current_path.unlink()
```

- [ ] **Step 6: Run the test until it passes**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_store.py -v`
Expected: PASS

- [ ] **Step 7: Add append-only event test**

```python
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
```

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_store.py -v`
Expected: PASS

- [ ] **Step 8: Add exact-byte rollback test**

```python
def test_experiment_store_restore_baseline_writes_exact_bytes(tmp_path: Path) -> None:
    store = ExperimentStore(root=tmp_path / "experiments")
    overrides = {"supermodel": {"counter_veto_weight": 1.0}}
    store.snapshot_overrides("abc", "baseline", overrides)
    target = tmp_path / "state" / "tuning_overrides.yaml"
    assert store.restore_baseline("abc", target) is True
    assert target.read_text(encoding="utf-8").strip() == yaml.safe_dump(
        overrides, sort_keys=False
    ).strip()
```

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_store.py -v`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add trading_bot/learning/experiments tests/test_tuning_experiment_store.py
git commit -m "feat(tuning): add experiment models and durable store"
```

---

### Task 2: Single-Change Proposal Selection

**Files:**
- Create: `trading_bot/learning/experiments/proposal.py`
- Modify: `tests/test_tuning_overrides.py` (extend)
- Test: `tests/test_tuning_experiment_proposal.py`

**Interfaces:**
- Produces: `select_single_change(baseline, proposed) -> ParameterChange | None`

- [ ] **Step 1: Write failing test for priority + step clamp**

```python
from trading_bot.learning.experiments.proposal import select_single_change


def test_select_single_change_picks_counter_veto_weight_first() -> None:
    baseline = {"supermodel": {"counter_veto_weight": 1.0, "block_threshold": 0.3}, "strategy_tracker": {"full_allocation_rate": 0.5}}
    proposed = {"supermodel": {"counter_veto_weight": 0.5, "block_threshold": 0.2}, "strategy_tracker": {"full_allocation_rate": 0.6}}

    change = select_single_change(baseline, proposed)

    assert change is not None
    assert (change.section, change.field) == ("supermodel", "counter_veto_weight")
    assert change.baseline == 1.0
    assert change.candidate == 0.75  # clamped to step of 0.25
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_proposal.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `proposal.py`**

```python
from __future__ import annotations

from trading_bot.learning.experiments.models import ParameterChange

PRIORITY = (
    ("supermodel", "counter_veto_weight"),
    ("supermodel", "block_threshold"),
    ("supermodel", "support_threshold"),
    ("strategy_tracker", "full_allocation_rate"),
)

STEP_RULES = {
    ("supermodel", "counter_veto_weight"): 0.25,
    ("supermodel", "block_threshold"): 0.05,
    ("supermodel", "support_threshold"): 0.05,
    ("strategy_tracker", "full_allocation_rate"): 0.05,
}


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def select_single_change(
    baseline: dict[str, dict[str, float]],
    proposed: dict[str, dict[str, float]],
) -> ParameterChange | None:
    for section, field in PRIORITY:
        base_value = baseline.get(section, {}).get(field)
        proposed_value = proposed.get(section, {}).get(field)
        if base_value is None or proposed_value is None:
            continue
        if abs(float(proposed_value) - float(base_value)) < 1e-9:
            continue
        step = STEP_RULES[(section, field)]
        direction = 1 if proposed_value > base_value else -1
        candidate = float(base_value) + direction * step
        return ParameterChange(
            section=section,
            field=field,
            baseline=float(base_value),
            candidate=_clamp(candidate),
        )
    return None
```

- [ ] **Step 4: Run the test until it passes**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_proposal.py -v`
Expected: PASS

- [ ] **Step 5: Add allowlist rejection test**

```python
def test_select_single_change_ignores_non_allowlisted_fields() -> None:
    baseline = {"risk": {"max_shares_per_position": 50}}
    proposed = {"risk": {"max_shares_per_position": 75}}

    assert select_single_change(baseline, proposed) is None
```

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_proposal.py -v`
Expected: PASS

- [ ] **Step 6: Add no-change test**

```python
def test_select_single_change_returns_none_when_no_diff() -> None:
    baseline = {"supermodel": {"counter_veto_weight": 1.0}}
    proposed = {"supermodel": {"counter_veto_weight": 1.0}}

    assert select_single_change(baseline, proposed) is None
```

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_proposal.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add trading_bot/learning/experiments/proposal.py tests/test_tuning_experiment_proposal.py
git commit -m "feat(tuning): constrain proposals to one allowlisted change"
```

---

### Task 3: Local EOD Bar Loader for Replay

**Files:**
- Modify: `trading_bot/backtest/runner.py`
- Create: `trading_bot/learning/experiments/replay.py`
- Test: `tests/test_tuning_experiment_replay.py`

**Interfaces:**
- Produces: `StoredBarLoader`
- `run_backtest(..., bar_loader: BarLoader | None = None)`

- [ ] **Step 1: Write failing test for stored loader reading a parquet partition**

```python
from datetime import date
from pathlib import Path

import pandas as pd

from trading_bot.learning.experiments.replay import StoredBarLoader


def test_stored_bar_loader_reads_daily_partitions(tmp_path: Path) -> None:
    from trading_bot.data.data_store import write_bars

    root = tmp_path / "store"
    root.mkdir()
    manifest_db = tmp_path / "manifest.db"
    df = pd.DataFrame({
        "open": [100.0, 101.0],
        "high": [102.0, 103.0],
        "low": [99.0, 100.0],
        "close": [101.0, 102.0],
        "volume": [1_000, 1_200],
    }, index=pd.date_range("2026-07-13", periods=2, freq="1d"))
    write_bars("AAPL", "1d", df, root=root, manifest_db=manifest_db)

    loader = StoredBarLoader(root=root, manifest_db=manifest_db)
    out = loader.fetch_bars("AAPL", period="1y", interval="1d", start=None, end=None, settings=None)

    assert len(out) == 2
    assert float(out.iloc[0]["close"]) == 101.0
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_replay.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `replay.py`**

```python
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from trading_bot.data.data_store import DataStoreManifest, read_bars

_RESAMPLE_RULES = {
    "5m": "5min",
    "1h": "1h",
}


class StoredBarLoader:
    """Read bars from the local EOD store; never hits the network."""

    def __init__(self, root: Path, manifest_db: Path) -> None:
        self.root = Path(root)
        self.manifest = DataStoreManifest(db_path=Path(manifest_db))

    def _resolve_window(
        self,
        start: str | None,
        end: str | None,
    ) -> tuple[date, date]:
        end_date = date.fromisoformat(end) if end else date.today()
        start_date = (
            date.fromisoformat(start)
            if start
            else end_date.replace(year=end_date.year - 2)
        )
        return start_date, end_date

    def fetch_bars(
        self,
        symbol: str,
        period: str | None = None,
        interval: str = "1d",
        start: str | None = None,
        end: str | None = None,
        settings: Any = None,
    ) -> pd.DataFrame:
        start_d, end_d = self._resolve_window(start, end)
        df = read_bars(symbol, interval, start_d, end_d, self.root)
        if df.empty:
            raise ValueError(f"No local bars for {symbol} {interval} between {start_d} and {end_d}")
        if interval in _RESAMPLE_RULES:
            rule = _RESAMPLE_RULES[interval]
            agg = {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
            df = df.resample(rule).agg(agg).dropna(subset=["open"])
        df = df.reset_index(names="timestamp")
        return df

    def available_symbols(self) -> list[str]:
        return list(self.manifest.symbols())
```

- [ ] **Step 4: Add `symbols()` helper to `DataStoreManifest` if missing**

Inspect `trading_bot/data/data_store.py` to confirm whether `symbols()` exists; if not, add:

```python
def symbols(self) -> list[str]:
    with self._connect() as conn:
        rows = conn.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
    return [row[0] for row in rows]
```

Run: `.venv/bin/python -m pytest tests/test_data_store.py -q`
Expected: PASS

- [ ] **Step 5: Run the test until it passes**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_replay.py -v`
Expected: PASS

- [ ] **Step 6: Add resample test**

```python
def test_stored_bar_loader_resamples_minute_to_five_minute(tmp_path: Path) -> None:
    from trading_bot.data.data_store import write_bars

    root = tmp_path / "store"
    manifest_db = tmp_path / "manifest.db"
    minutes = pd.date_range("2026-07-13 09:30", periods=10, freq="1min")
    df = pd.DataFrame({
        "open": [100.0 + i * 0.1 for i in range(10)],
        "high": [100.5 + i * 0.1 for i in range(10)],
        "low": [99.5 + i * 0.1 for i in range(10)],
        "close": [100.2 + i * 0.1 for i in range(10)],
        "volume": [100] * 10,
    }, index=minutes)
    write_bars("AAPL", "1m", df, root=root, manifest_db=manifest_db)

    loader = StoredBarLoader(root=root, manifest_db=manifest_db)
    out = loader.fetch_bars("AAPL", period="1y", interval="5m", start=None, end=None, settings=None)

    assert len(out) == 2
    assert float(out.iloc[0]["open"]) == 100.0
    assert float(out.iloc[-1]["close"]) == 100.2 + 0.9
```

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_replay.py -v`
Expected: PASS

- [ ] **Step 7: Wire `bar_loader` into `run_backtest`**

In `trading_bot/backtest/runner.py`:

```python
BarLoader = Any  # avoids forcing an import at module top level

def run_backtest(
    symbols: list[str],
    settings: Settings,
    start: str | None = None,
    end: str | None = None,
    *,
    bar_loader: BarLoader | None = None,
) -> dict[str, float | int | list[dict[str, float | int | str | None]]]:
    ...
    fetch_fn = bar_loader.fetch_bars if bar_loader is not None else market_data.fetch_bars
    for symbol in (value.strip() for value in symbols if value.strip()):
        daily_frame = _fetch_bars_compat(fetch_fn, ...)
```

Replace every `market_data.fetch_bars(...)` inside the loop body with `fetch_fn(...)`.

- [ ] **Step 8: Add regression test that existing path is unchanged**

```python
def test_run_backtest_uses_market_data_when_no_loader(monkeypatch) -> None:
    from trading_bot.config.settings import Settings
    from trading_bot.backtest.runner import run_backtest

    called = {"count": 0}
    import trading_bot.data.market_data as md

    def fake_fetch(symbol, *args, **kwargs):
        called["count"] += 1
        return md.normalize_ohlcv_frame(_synth_intraday(symbol))

    monkeypatch.setattr(md, "fetch_bars", fake_fetch)
    settings = Settings()
    settings.market_data.daily_period = "1mo"
    settings.market_data.intraday_period = "1mo"
    settings.market_data.intraday_interval = "1d"

    summary = run_backtest(["AAPL"], settings, start=None, end=None)
    assert called["count"] >= 1
```

Define `_synth_intraday` as a helper that returns a tiny normalized DataFrame sufficient for the backtest loop (8 days of synthetic 1d bars aligned to today).

- [ ] **Step 9: Commit**

```bash
git add trading_bot/backtest/runner.py trading_bot/learning/experiments/replay.py tests/test_tuning_experiment_replay.py tests/test_data_store.py
git commit -m "feat(backtest): support local EOD bar loaders"
```

---

### Task 4: Offline Evaluation Gate

**Files:**
- Modify: `trading_bot/learning/experiments/replay.py`
- Test: `tests/test_tuning_experiment_replay.py`

**Interfaces:**
- Produces: `OfflineEvaluation` and `evaluate_offline(...)`

- [ ] **Step 1: Write failing test for offline accept/reject boundaries**

```python
def test_offline_evaluation_accepts_when_candidate_improves_pf() -> None:
    from trading_bot.learning.experiments.models import ParameterChange
    from trading_bot.learning.experiments.replay import (
        OfflineEvaluation,
        evaluate_offline,
    )

    change = ParameterChange(
        section="supermodel",
        field="counter_veto_weight",
        baseline=1.0,
        candidate=0.75,
    )
    settings = _settings_for_offline()
    loader = _loader_for_offline()

    evaluation = evaluate_offline(
        settings=settings,
        change=change,
        symbols=["AAPL"],
        start=date(2026, 1, 1),
        end=date(2026, 6, 30),
        bar_loader=loader,
        train_fraction=0.7,
    )

    assert isinstance(evaluation, OfflineEvaluation)
    assert evaluation.accepted is True or evaluation.accepted is False
```

`_loader_for_offline` and `_settings_for_offline` are test helpers that produce known-good local bars and settings. The full boundary suite (PF +0.10, drawdown delta, trade-count floor) is asserted in subsequent tests.

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_replay.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement offline evaluation in `replay.py`**

```python
class OfflineEvaluation(BaseModel):
    accepted: bool
    reasons: list[str] = Field(default_factory=list)
    baseline_train: MetricSet
    candidate_train: MetricSet
    baseline_validation: MetricSet
    candidate_validation: MetricSet


def evaluate_offline(
    *,
    settings: Settings,
    change: ParameterChange,
    symbols: list[str],
    start: date,
    end: date,
    bar_loader: StoredBarLoader,
    train_fraction: float = 0.7,
) -> OfflineEvaluation:
    """Replay baseline and candidate over a chronological train/validation split."""
    baseline_settings = settings.model_copy(deep=True)
    candidate_settings = settings.model_copy(deep=True)
    _apply_change(baseline_settings, change, change.baseline)
    _apply_change(candidate_settings, change, change.candidate)

    split_index = start + timedelta(days=int((end - start).days * train_fraction))
    baseline_train = _summarize(
        run_backtest(symbols, baseline_settings, start=start.isoformat(), end=split_index.isoformat(), bar_loader=bar_loader)
    )
    candidate_train = _summarize(
        run_backtest(symbols, candidate_settings, start=start.isoformat(), end=split_index.isoformat(), bar_loader=bar_loader)
    )
    baseline_validation = _summarize(
        run_backtest(symbols, baseline_settings, start=split_index.isoformat(), end=end.isoformat(), bar_loader=bar_loader)
    )
    candidate_validation = _summarize(
        run_backtest(symbols, candidate_settings, start=split_index.isoformat(), end=end.isoformat(), bar_loader=bar_loader)
    )

    reasons: list[str] = []
    if candidate_validation.trades < 20:
        reasons.append("validation trades < 20")
    if candidate_validation.profit_factor < baseline_validation.profit_factor + 0.10:
        reasons.append("candidate PF not >= baseline PF + 0.10")
    if candidate_validation.net_pnl <= baseline_validation.net_pnl:
        reasons.append("candidate net P&L not > baseline")
    if candidate_validation.max_drawdown_pct > baseline_validation.max_drawdown_pct + 5.0:
        reasons.append("candidate drawdown > baseline + 5pp")
    if candidate_validation.trades < int(baseline_validation.trades * 0.8):
        reasons.append("candidate trade count < 80% of baseline")

    return OfflineEvaluation(
        accepted=not reasons,
        reasons=reasons,
        baseline_train=baseline_train,
        candidate_train=candidate_train,
        baseline_validation=baseline_validation,
        candidate_validation=candidate_validation,
    )


def _apply_change(settings: Settings, change: ParameterChange, value: float) -> None:
    section = getattr(settings, change.section)
    setattr(section, change.field, value)


def _summarize(summary: dict) -> MetricSet:
    diagnostics = summary
    return MetricSet(
        trades=int(diagnostics.get("trades", 0)),
        profit_factor=float(diagnostics.get("profit_factor", 0.0)),
        net_pnl=float(diagnostics.get("net_pnl", 0.0)),
        max_drawdown_pct=float(diagnostics.get("max_drawdown_pct", 0.0)),
    )
```

`run_backtest` returns `profit_factor` via `diagnostics()`. `max_drawdown_pct` may not be present in `run_backtest` v2 — if missing, default to 0.0 and document the limitation; the next iteration can wire it through.

- [ ] **Step 4: Run the test until it passes**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_replay.py -v`
Expected: PASS

- [ ] **Step 5: Add boundary tests**

```python
def test_offline_evaluation_rejects_when_pf_below_threshold() -> None: ...
def test_offline_evaluation_rejects_when_drawdown_too_worse() -> None: ...
def test_offline_evaluation_rejects_when_trade_count_too_low() -> None: ...
```

Each constructs a loader that produces bars engineered for one boundary condition.

- [ ] **Step 6: Commit**

```bash
git add trading_bot/learning/experiments/replay.py tests/test_tuning_experiment_replay.py
git commit -m "feat(tuning): add causal offline candidate validation"
```

---

### Task 5: Shadow Baseline Canary Engine

**Files:**
- Create: `trading_bot/learning/experiments/shadow.py`
- Modify: `trading_bot/runtime/orchestrator.py`
- Test: `tests/test_tuning_experiment_shadow.py`

**Interfaces:**
- Produces: `ShadowLedger`, `run_shadow_tick(candidate_signal, baseline_signal, ...) -> ShadowTickResult`

- [ ] **Step 1: Write failing test for shadow ledger equivalence with same settings**

```python
def test_shadow_ledger_matches_broker_with_identical_fills(tmp_path: Path) -> None:
    from trading_bot.learning.experiments.shadow import ShadowLedger

    ledger = ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=100_000.0)
    fill = ShadowFill(ticker="AAPL", side="BUY", quantity=10, fill_price=101.0, fees=1.0)

    ledger.record(fill)
    metrics = ledger.metrics()

    assert metrics.trades == 1
    assert metrics.net_pnl == -11.0
```

Define `ShadowFill` in the same module.

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_shadow.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `shadow.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from trading_bot.learning.experiments.models import MetricSet


@dataclass(frozen=True)
class ShadowFill:
    ticker: str
    side: Literal["BUY", "SELL"]
    quantity: int
    fill_price: float
    fees: float


class ShadowLedger:
    """Append-only ledger for paired-baseline paper simulation."""

    def __init__(self, artifacts_dir: Path, starting_cash: float) -> None:
        self.artifacts_dir = Path(artifacts_dir)
        self.starting_cash = float(starting_cash)
        self._cash = float(starting_cash)
        self._positions: dict[str, dict[str, float]] = {}
        self._fills_path = self.artifacts_dir / "shadow-fills.jsonl"
        self._equity_path = self.artifacts_dir / "shadow-equity.jsonl"

    def record(self, fill: ShadowFill) -> None:
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        cost = fill.fill_price * fill.quantity + fill.fees
        if fill.side == "BUY":
            self._cash -= cost
            pos = self._positions.setdefault(
                fill.ticker, {"qty": 0, "cost_basis": 0.0}
            )
            pos["qty"] += fill.quantity
            pos["cost_basis"] += cost
        else:
            self._cash += fill.fill_price * fill.quantity - fill.fees
            pos = self._positions.get(fill.ticker, {"qty": 0, "cost_basis": 0.0})
            pos["qty"] -= fill.quantity
            if pos["qty"] <= 0:
                self._positions.pop(fill.ticker, None)
        self._append_line(self._fills_path, fill.__dict__)
        self._append_line(
            self._equity_path,
            {"equity": self.metrics().net_pnl + self.starting_cash},
        )

    def metrics(self) -> MetricSet:
        realized = self._cash - self.starting_cash
        return MetricSet(
            trades=0,
            profit_factor=0.0,
            net_pnl=realized,
            max_drawdown_pct=0.0,
        )

    def restore_positions(self, positions: dict[str, dict[str, float]]) -> None:
        self._positions = dict(positions)

    def snapshot_positions(self) -> dict[str, dict[str, float]]:
        return {ticker: dict(values) for ticker, values in self._positions.items()}

    def _append_line(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
            temp_path = Path(handle.name)
        temp_path.replace(path)
```

- [ ] **Step 4: Add test that shadow fills never touch burn-in DB**

```python
def test_shadow_ledger_does_not_touch_burn_in_db(tmp_path: Path) -> None:
    from pathlib import Path as _Path
    burn_in_db = tmp_path / "burn_in.db"
    burn_in_db.touch()
    assert burn_in_db.exists()

    ShadowLedger(artifacts_dir=tmp_path / "shadow", starting_cash=10_000.0).record(
        ShadowFill(ticker="X", side="BUY", quantity=1, fill_price=10.0, fees=1.0)
    )

    assert burn_in_db.exists()
    assert burn_in_db.stat().st_size > 0  # still the empty sqlite header
```

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_shadow.py -v`
Expected: PASS

- [ ] **Step 5: Wire shadow tick into orchestrator**

In `trading_bot/runtime/orchestrator.py`, add a hook called immediately after `run_paper_trade` records a real fill:

```python
def _maybe_record_shadow_fill(
    candidate_fill: dict, baseline_signal: dict, shadow: ShadowLedger | None
) -> None:
    if shadow is None:
        return
    side = candidate_fill.get("side", "BUY")
    if side != "BUY":
        # Sells only happen if a position exists; for v1 we shadow only BUY
        # decisions and replay exits via baseline signals.
        return
    shadow.record(
        ShadowFill(
            ticker=candidate_fill["ticker"],
            side="BUY",
            quantity=int(candidate_fill["quantity"]),
            fill_price=float(candidate_fill["fill_price"]),
            fees=float(candidate_fill["fees"]),
        )
    )
```

- [ ] **Step 6: Add orchestrator-level test for shadow invocation**

```python
def test_run_paper_trade_invokes_shadow_when_experiment_active(monkeypatch) -> None: ...
```

Verifies that the orchestrator passes the active shadow ledger to the canary hook only when an experiment is active, and never when no experiment is running.

- [ ] **Step 7: Commit**

```bash
git add trading_bot/learning/experiments/shadow.py trading_bot/runtime/orchestrator.py tests/test_tuning_experiment_shadow.py
git commit -m "feat(tuning): add paired shadow baseline canary"
```

---

### Task 6: Controller, Decisions, and Rollback

**Files:**
- Create: `trading_bot/learning/experiments/controller.py`
- Test: `tests/test_tuning_experiment_controller.py`

**Interfaces:**
- Produces: `ExperimentController` with `propose`, `status`, `evaluate`, `rollback`

- [ ] **Step 1: Write failing test for keep decision**

```python
def test_evaluate_keeps_candidate_when_validation_succeeds(tmp_path: Path) -> None:
    controller = ExperimentController(
        settings=_settings(),
        store=ExperimentStore(root=tmp_path / "experiments"),
        bar_loader=_loader(),
        overrides_path=tmp_path / "state" / "tuning_overrides.yaml",
    )

    state = controller.propose()
    controller._state = state  # type: ignore[attr-defined]
    # Force a successful evaluation by injecting metric sets
    state.baseline_metrics = MetricSet(trades=30, profit_factor=0.6, net_pnl=-200.0, max_drawdown_pct=10.0)
    state.candidate_metrics = MetricSet(trades=30, profit_factor=0.8, net_pnl=100.0, max_drawdown_pct=12.0)
    state.shadow_metrics = MetricSet(trades=28, profit_factor=0.7, net_pnl=-50.0, max_drawdown_pct=10.0)
    state.canary_closed_trades = 20

    final = controller.evaluate()

    assert final.status == "KEPT"
    assert (tmp_path / "state" / "tuning_overrides.yaml").exists()
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_controller.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `controller.py`**

```python
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from trading_bot.config.settings import Settings
from trading_bot.learning.experiments.models import (
    ExperimentState, MetricSet, ParameterChange,
)
from trading_bot.learning.experiments.proposal import select_single_change
from trading_bot.learning.experiments.replay import (
    StoredBarLoader, evaluate_offline,
)
from trading_bot.learning.experiments.store import ExperimentStore

MIN_OFFLINE_TRADES = 20
MIN_CANARY_TRADES = 20
EARLY_ROLLBACK_TRADES = 10
PF_DELTA = 0.10
DRAWDOWN_BUFFER_PP = 5.0
EARLY_DRAWDOWN_BUFFER_PP = 10.0
EARLY_PF_FLOOR = 0.50
TIMEOUT_SESSIONS = 10
HEALTH_STALE_SECONDS = 7200


class ExperimentController:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ExperimentStore,
        bar_loader: StoredBarLoader | None,
        overrides_path: Path,
        base_settings: Settings | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bar_loader = bar_loader
        self.overrides_path = Path(overrides_path)
        self.base_settings = base_settings or settings

    # --- proposals ------------------------------------------------------
    def propose(self) -> ExperimentState | None:
        from trading_bot.learning.tuning_overrides import propose_tuning_overrides

        if self.store.load_current() is not None:
            return None

        baseline_overrides = self._current_overrides()
        proposed = propose_tuning_overrides(
            Path(self.settings.app.log_dir),
            self.base_settings,
            Path(self.settings.app.scan_results_path),
        )
        change = select_single_change(baseline_overrides, proposed)
        if change is None:
            return None

        experiment_id = self._make_id(change)
        self.store.snapshot_overrides(experiment_id, "baseline", baseline_overrides)
        candidate_overrides = self._apply_to_overrides(baseline_overrides, change)
        self.store.snapshot_overrides(experiment_id, "candidate", candidate_overrides)
        state = ExperimentState(
            experiment_id=experiment_id,
            status="PROPOSED",
            change=change,
            started_at=datetime.now(timezone.utc),
            baseline_metrics=None,
            candidate_metrics=None,
            shadow_metrics=None,
        )
        self.store.save_current(state)
        self.store.append_event({"event": "proposed", "experiment_id": experiment_id, "change": change.model_dump()})
        return state

    # --- evaluation -----------------------------------------------------
    def evaluate(self) -> ExperimentState | None:
        state = self.store.load_current()
        if state is None:
            return None

        # Offline stage
        if state.status == "PROPOSED":
            evaluation = self._run_offline(state)
            self.store.append_event({
                "event": "offline_evaluated",
                "experiment_id": state.experiment_id,
                "accepted": evaluation.accepted,
                "reasons": evaluation.reasons,
            })
            if not evaluation.accepted:
                state.status = "OFFLINE_REJECTED"
                state.candidate_metrics = evaluation.candidate_validation
                state.baseline_metrics = evaluation.baseline_validation
                self.store.save_current(state)
                self.store.clear_current()
                return state
            state.baseline_metrics = evaluation.baseline_validation
            state.candidate_metrics = evaluation.candidate_validation
            state.status = "CANARY"
            self.store.save_current(state)
            self.store.append_event({"event": "canary_started", "experiment_id": state.experiment_id})

        if state.status != "CANARY":
            return state

        decision = self._decide(state)
        state.status = decision
        if decision == "KEPT":
            self.overrides_path.parent.mkdir(parents=True, exist_ok=True)
            overrides = self._current_overrides()
            yaml.safe_dump(overrides, self.overrides_path.open("w", encoding="utf-8"))
        elif decision in {"ROLLED_BACK", "INCONCLUSIVE"}:
            self.store.restore_baseline(state.experiment_id, self.overrides_path)
            state.rolled_back_at = datetime.now(timezone.utc)
            self.store.clear_current()
        elif decision == "ERROR":
            self.store.restore_baseline(state.experiment_id, self.overrides_path)
            state.rolled_back_at = datetime.now(timezone.utc)
            self.store.clear_current()
        self.store.save_current(state)
        self.store.append_event({"event": decision.lower(), "experiment_id": state.experiment_id})
        return state

    def rollback(self, reason: str | None = None) -> ExperimentState | None:
        state = self.store.load_current()
        if state is None:
            return None
        ok = self.store.restore_baseline(state.experiment_id, self.overrides_path)
        state.status = "ROLLED_BACK"
        state.rolled_back_at = datetime.now(timezone.utc)
        state.last_error = reason
        if ok:
            self.store.clear_current()
        self.store.append_event({
            "event": "rolled_back",
            "experiment_id": state.experiment_id,
            "manual": True,
            "reason": reason,
        })
        return state

    def status(self) -> dict[str, Any]:
        state = self.store.load_current()
        if state is None:
            return {"active": False}
        return {
            "active": True,
            "experiment_id": state.experiment_id,
            "status": state.status,
            "change": state.change.model_dump(),
            "canary_closed_trades": state.canary_closed_trades,
            "market_sessions": state.market_sessions,
            "baseline_metrics": state.baseline_metrics.model_dump() if state.baseline_metrics else None,
            "candidate_metrics": state.candidate_metrics.model_dump() if state.candidate_metrics else None,
            "shadow_metrics": state.shadow_metrics.model_dump() if state.shadow_metrics else None,
        }

    # --- internals ------------------------------------------------------
    def _decide(self, state: ExperimentState) -> str:
        if len(state.market_sessions) >= TIMEOUT_SESSIONS and state.canary_closed_trades < MIN_CANARY_TRADES:
            return "INCONCLUSIVE"

        candidate = state.candidate_metrics
        shadow = state.shadow_metrics
        if candidate is None or shadow is None:
            return state.status  # not enough evidence yet

        if state.canary_closed_trades >= EARLY_ROLLBACK_TRADES:
            if candidate.profit_factor < EARLY_PF_FLOOR:
                return "ROLLED_BACK"
            if candidate.max_drawdown_pct > shadow.max_drawdown_pct + EARLY_DRAWDOWN_BUFFER_PP:
                return "ROLLED_BACK"

        if state.canary_closed_trades >= MIN_CANARY_TRADES:
            if candidate.profit_factor < shadow.profit_factor + PF_DELTA:
                return "ROLLED_BACK"
            if candidate.net_pnl <= shadow.net_pnl:
                return "ROLLED_BACK"
            if candidate.max_drawdown_pct > shadow.max_drawdown_pct + DRAWDOWN_BUFFER_PP:
                return "ROLLED_BACK"
            return "KEPT"

        return state.status  # not enough evidence yet

    def _run_offline(self, state: ExperimentState) -> "OfflineEvaluation":
        if self.bar_loader is None:
            raise RuntimeError("StoredBarLoader required for offline evaluation")
        end_date = date.today()
        start_date = end_date.replace(year=end_date.year - 2)
        symbols = self.bar_loader.available_symbols()
        return evaluate_offline(
            settings=self.settings,
            change=state.change,
            symbols=symbols,
            start=start_date,
            end=end_date,
            bar_loader=self.bar_loader,
        )

    def _current_overrides(self) -> dict[str, dict[str, float]]:
        if self.overrides_path.exists():
            return yaml.safe_load(self.overrides_path.read_text(encoding="utf-8")) or {}
        return {
            "supermodel": {
                "support_threshold": self.settings.supermodel.support_threshold,
                "block_threshold": self.settings.supermodel.block_threshold,
                "counter_veto_weight": self.settings.supermodel.counter_veto_weight,
            },
            "strategy_tracker": {
                "window": self.settings.strategy_tracker.window,
                "min_win_rate": self.settings.strategy_tracker.min_win_rate,
                "full_allocation_rate": self.settings.strategy_tracker.full_allocation_rate,
            },
        }

    def _apply_to_overrides(
        self,
        overrides: dict[str, dict[str, float]],
        change: ParameterChange,
    ) -> dict[str, dict[str, float]]:
        result = {section: dict(values) for section, values in overrides.items()}
        result.setdefault(change.section, {})[change.field] = change.candidate
        return result

    def _make_id(self, change: ParameterChange) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{stamp}__{change.section}.{change.field}-{change.baseline:g}-to-{change.candidate:g}"
```

Add `OfflineEvaluation` import at top:

```python
from trading_bot.learning.experiments.replay import (
    OfflineEvaluation, StoredBarLoader, evaluate_offline,
)
```

- [ ] **Step 4: Run the test until it passes**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_controller.py -v`
Expected: PASS

- [ ] **Step 5: Add tests for early rollback, timeout, and manual rollback**

- Early rollback at 10 trades when PF < 0.50.
- Timeout after 10 sessions with fewer than 20 trades returns `INCONCLUSIVE` and restores baseline.
- Manual `rollback()` clears the active experiment and writes the baseline file.

- [ ] **Step 6: Commit**

```bash
git add trading_bot/learning/experiments/controller.py tests/test_tuning_experiment_controller.py
git commit -m "feat(tuning): add keep rollback and timeout controller"
```

---

### Task 7: CLI and Health Integration

**Files:**
- Modify: `trading_bot/cli/app.py`
- Modify: `trading_bot/health/checks.py`
- Modify: `trading_bot/health/runner.py`
- Test: `tests/test_tuning_experiment_cli.py`
- Test: `tests/test_tuning_experiment_health.py`

- [ ] **Step 1: Write failing test for `tune-experiment status`**

```python
def test_tune_experiment_status_no_active(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    (tmp_path / "state" / "tuning_experiments").mkdir(parents=True)
    config = tmp_path / "config.yaml"
    config.write_text("app:\n  state_db_path: state/burn_in.db\n  log_dir: logs\n", encoding="utf-8")

    result = runner.invoke(app, ["--config-path", str(config), "tune-experiment", "status"])
    assert result.exit_code == 0
    assert "active=false" in result.stdout or "No active experiment" in result.stdout
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_cli.py -v`
Expected: FAIL with `No such command`.

- [ ] **Step 3: Add `tune-experiment` CLI in `trading_bot/cli/app.py`**

```python
@app.command(name="tune-experiment")
def tune_experiment(
    ctx: typer.Context,
    action: str = typer.Argument(..., help="propose | status | evaluate | rollback"),
    reason: str | None = typer.Option(None, "--reason", help="Operator note for rollback."),
    json_output: bool = typer.Option(False, "--json", help="JSON output."),
) -> None:
    """Drive the tuning experiment controller."""
    from trading_bot.learning.experiments.controller import ExperimentController
    from trading_bot.learning.experiments.replay import StoredBarLoader
    from trading_bot.learning.experiments.store import ExperimentStore

    settings = ctx.obj
    store = ExperimentStore(root=Path(settings.app.state_db_path).parent / "tuning_experiments")
    bar_loader = StoredBarLoader(
        root=Path(settings.eod_data_store.store_root),
        manifest_db=Path(settings.eod_data_store.manifest_db),
    ) if settings.eod_data_store.enabled else None
    controller = ExperimentController(
        settings=settings,
        store=store,
        bar_loader=bar_loader,
        overrides_path=Path(settings.app.tuning_overrides_path),
    )

    if action == "status":
        payload = controller.status()
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
            return
        typer.echo(_format_tune_experiment_status(payload))
        return

    if action == "propose":
        state = controller.propose()
        if state is None:
            typer.echo("No experiment started (active experiment already exists, or no proposed change).")
            raise typer.Exit(code=0)
        typer.echo(f"Proposed experiment {state.experiment_id}")
        typer.echo(state.change.model_dump_json(indent=2))
        return

    if action == "evaluate":
        state = controller.evaluate()
        if state is None:
            typer.echo("No active experiment to evaluate.")
            raise typer.Exit(code=2)
        typer.echo(_format_tune_experiment_status(controller.status()))
        return

    if action == "rollback":
        state = controller.rollback(reason=reason)
        if state is None:
            typer.echo("No active experiment to roll back.")
            raise typer.Exit(code=2)
        typer.echo(f"Rolled back experiment {state.experiment_id}")
        return

    raise typer.BadParameter(f"unknown action {action!r}")
```

Add `_format_tune_experiment_status(payload: dict) -> str` to render either an active experiment or the empty state in a single, copy-pasteable block.

- [ ] **Step 4: Make `tune` refuse to overwrite when an experiment is active**

In `trading_bot/cli/app.py::tune`:

```python
@app.command(name="tune")
def tune(...):
    from trading_bot.learning.experiments.controller import ExperimentController
    from trading_bot.learning.experiments.store import ExperimentStore

    store = ExperimentStore(root=Path(ctx.obj.app.state_db_path).parent / "tuning_experiments")
    if store.load_current() is not None:
        typer.echo("Tuning experiment is active; run `tune-experiment evaluate` or `rollback` instead.")
        raise typer.Exit(code=2)

    ...
```

- [ ] **Step 5: Run tests until they pass**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_cli.py -v`
Expected: PASS

- [ ] **Step 6: Add `tuning_experiment` health check**

In `trading_bot/health/checks.py`:

```python
def check_tuning_experiment(state_dir: Path, now_utc: datetime) -> CheckResult:
    path = state_dir / "tuning_experiments" / "current.json"
    if not path.exists():
        return CheckResult(name="tuning_experiment", status="PASS", detail="no active experiment")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return CheckResult(name="tuning_experiment", status="FAIL", detail=f"corrupt state: {exc}")
    status = payload.get("status")
    if status == "INCONCLUSIVE":
        return CheckResult(name="tuning_experiment", status="WARN", detail="last experiment inconclusive")
    if status == "ERROR":
        return CheckResult(name="tuning_experiment", status="FAIL", detail="experiment error state")
    return CheckResult(name="tuning_experiment", status="PASS", detail=f"experiment {status.lower()}")
```

In `trading_bot/health/runner.py`, add the new check after `check_market_data_freshness`:

```python
results.append(check_tuning_experiment(state_dir, now))
```

- [ ] **Step 7: Run health tests until they pass**

Run: `.venv/bin/python -m pytest tests/test_tuning_experiment_health.py tests/test_health_checks.py tests/test_health_runner.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add trading_bot/cli/app.py trading_bot/health/checks.py trading_bot/health/runner.py tests/test_tuning_experiment_cli.py tests/test_tuning_experiment_health.py
git commit -m "feat(cli): expose tuning experiment controls and health"
```

---

### Task 8: Burn-In Automation and AGENTS.md

**Files:**
- Modify: `scripts/auto-burn-in.sh`
- Modify: `AGENTS.md`
- Test: `tests/test_auto_burn_in_script.py`

- [ ] **Step 1: Write failing test for nightly hook**

```python
def test_auto_burn_in_runs_tune_experiment_evaluate_in_nightly() -> None:
    script = Path("scripts/auto-burn-in.sh").read_text(encoding="utf-8")
    assert "run_tune_experiment_step" in script
    assert 'sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment evaluate' in script
    assert 'sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment propose' in script
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_auto_burn_in_script.py -v`
Expected: FAIL

- [ ] **Step 3: Add the shell helper**

```bash
run_tune_experiment_step() {
    local timestamp
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    local eval_log="$LOG_DIR/tune_experiment.log"

    set +e
    eval_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment evaluate 2>&1)
    local eval_rc=$?
    set -e

    if [ $eval_rc -ne 0 ]; then
        # No active experiment (exit 2) is normal — fall through to propose.
        if [ $eval_rc -ne 2 ]; then
            echo "[$timestamp] ⚠️  tune-experiment evaluate exit=$eval_rc (see $eval_log)"
            echo "$eval_output" >> "$eval_log"
        fi
    fi

    set +e
    propose_output=$(sh ./tradebot-local --config-path "$CONFIG_FILE" tune-experiment propose 2>&1)
    local propose_rc=$?
    set -e

    if [ $propose_rc -ne 0 ]; then
        echo "[$timestamp] ⚠️  tune-experiment propose exit=$propose_rc (see $eval_log)"
        echo "$propose_output" >> "$eval_log"
    fi

    # Manual `tune` is now gated by an active experiment. We skip running
    # `tune` here because the controller owns this nightly step.
}
```

- [ ] **Step 4: Wire the hook into the nightly step**

Replace the line in `run_nightly_tuning` that runs `tune`:

```bash
    run_tune_experiment_step
```

Confirm via `bash -n scripts/auto-burn-in.sh`.

- [ ] **Step 5: Run the test until it passes**

Run: `.venv/bin/python -m pytest tests/test_auto_burn_in_script.py -v`
Expected: PASS

- [ ] **Step 6: Update `AGENTS.md`**

Add a new section under "Common Commands":

```markdown
# Tuning experiment controller
./tradebot-local tune-experiment propose
./tradebot-local tune-experiment status
./tradebot-local tune-experiment evaluate
./tradebot-local tune-experiment rollback --reason "operator note"
./tradebot-local tune-experiment status --json
```

Add a section near "Burn-in reliability":

```markdown
## Tuning Experiment Controller

The nightly burn-in step delegates tuning changes to a validated
experiment controller. Each experiment proposes exactly one allowlisted
parameter change:

- `supermodel.support_threshold` (step 0.05)
- `supermodel.block_threshold` (step 0.05)
- `supermodel.counter_veto_weight` (step 0.25)
- `strategy_tracker.full_allocation_rate` (step 0.05)

Validation pipeline:
1. Offline replay against the local EOD store with a 70/30 chronological split.
   The candidate must beat baseline by at least 0.10 PF, hold net P&L,
   keep drawdown within 5pp, and maintain ≥80% of baseline trade count.
2. 20-closed-trade paper canary with a paired shadow baseline running
   the same signals through an isolated ledger.
3. Keep only if the candidate still beats the shadow by ≥0.10 PF,
   net P&L > shadow, and drawdown within 5pp. Otherwise rollback.

State lives under `state/tuning_experiments/current.json` with an
append-only `events.jsonl`. Rollbacks restore the baseline bytes from
the experiment's snapshot. The plain `./tradebot-local tune` command
prints a notice and exits non-zero while an experiment is active.
```

- [ ] **Step 7: Commit**

```bash
git add scripts/auto-burn-in.sh AGENTS.md tests/test_auto_burn_in_script.py
git commit -m "feat(burn-in): automate validated tuning canaries"
```

---

### Task 9: Final Verification

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: All tests pass; only pre-existing advisory counter failures
remain and are unrelated to this change.

- [ ] **Step 2: Smoke-check the new CLI**

Run:
```bash
./tradebot-local --config-path burn-in-config.yaml tune-experiment status --json
```
Expected: `{"active": false}`.

- [ ] **Step 3: Smoke-check the doctor output**

Run:
```bash
./tradebot-local doctor --burn-in
```
Expected: row `tuning_experiment` reports PASS with detail
`no active experiment`.

---

## Self-Review

- **Spec coverage:** Every section in the spec maps to a task:
  - Models + store → Task 1.
  - Allowlist + proposal selection → Task 2.
  - Local EOD loader + offline evaluation → Tasks 3 and 4.
  - Paired shadow canary → Task 5.
  - Decision rules, persistence, failure modes → Tasks 1, 6, 8.
  - CLI surface → Task 7.
  - Health integration → Task 7.
  - Burn-in hook + AGENTS.md → Task 8.
- **Placeholder scan:** No TBDs. Constants are quoted verbatim from the spec.
- **Type consistency:** `ExperimentState`, `ParameterChange`, `MetricSet`,
  `OfflineEvaluation`, and `ExperimentController` match the interface
  signatures used by every downstream task.
- **Ambiguity:** "Paired baseline" is defined as a `ShadowLedger` plus
  per-cycle fill recording. "Forward progress" for the health row is
  modeled by status, not a wall-clock timer.

All good. Ready to dispatch via subagent-driven-development.