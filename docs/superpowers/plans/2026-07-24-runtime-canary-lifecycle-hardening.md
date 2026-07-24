# Runtime Canary Lifecycle Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the paper-trading runtime canary load in production, collect exactly-once paired evidence, count completed positions, and restore baseline overrides immediately when tracking fails.

**Architecture:** A central lifecycle factory derives the canonical experiment store from `Settings`, reconciles durable canary-tagged order rows into idempotent shadow ledgers, and finalizes metrics once per command or loop cycle. The experiment controller owns one terminal transition for restoration and archival, while the execution layer persists canary metadata with each durable fill before notifying the shadow context.

**Tech Stack:** Python 3.11+, Pydantic, SQLite, JSONL, Typer, pytest

## Global Constraints

- Paper-only remains enforced; never enable live trading.
- Tests remain deterministic and network-free.
- Do not modify unrelated behavior or add support for additional tuning parameters.
- `supermodel.range_bound_trend_caution_multiplier` remains the only runtime-canary parameter.
- Runtime tracking failures restore baseline immediately but do not mutate the current process's loaded `Settings`.
- The 20-trade gate counts completed positions, not partial SELL fills.
- Update `AGENTS.md` and `ARCHITECTURE.md` with the corrected operational contract.

---

## File Structure

- `trading_bot/portfolio/ledger.py`: additive order metadata migration, durable canary row writes, and canary-order query.
- `trading_bot/learning/experiments/shadow.py`: idempotent paired fill replay and completed-position accounting.
- `trading_bot/learning/experiments/runtime_canary.py`: production lifecycle factory, reconciliation, finish, and immediate invalidation.
- `trading_bot/learning/experiments/controller.py`: fail-closed activation and unified terminal finalization.
- `trading_bot/runtime/orchestrator.py`: durable BUY metadata and post-transaction canary notification.
- `trading_bot/runtime/position_exit.py`: durable SELL metadata and post-transaction notification.
- `trading_bot/cli/app.py`: one begin/finish boundary per manual command.
- `trading_bot/runtime/continuous_loop.py`: one begin/finish boundary per cycle.
- `tests/test_runtime_canary_durable_orders.py`: order schema and query behavior.
- `tests/test_runtime_canary_idempotency.py`: replay, duplicates, partial exits, and completed-position counts.
- `tests/test_runtime_canary_lifecycle.py`: production loading, reconciliation, finish, and immediate rollback.
- `tests/test_runtime_canary_activation.py`: fail-closed activation and terminal outcomes.
- Existing `tests/test_runtime_canary_*.py`: update fixtures and assertions to the corrected public contract.
- `AGENTS.md`, `ARCHITECTURE.md`: operational documentation.

### Task 1: Durable Canary Order Metadata

**Files:**
- Modify: `trading_bot/portfolio/ledger.py:57-85,185-210`
- Create: `tests/test_runtime_canary_durable_orders.py`

**Interfaces:**
- Produces: `PortfolioLedger.record_fill(..., canary_experiment_id: str | None = None, canary_baseline_quantity: int | None = None) -> None`
- Produces: `PortfolioLedger.list_canary_order_rows(experiment_id: str) -> list[dict[str, object]]`
- Consumes: existing `FillResult.order_id` as the stable operation ID.

- [ ] **Step 1: Write failing migration and round-trip tests**

```python
def test_record_fill_persists_canary_metadata(tmp_path):
    ledger = PortfolioLedger(tmp_path / "state.db")
    fill = FillResult(order_id="buy-1", ticker="AAPL", quantity=5,
                      fill_price=100.0, fees=1.0,
                      filled_at=datetime.now(timezone.utc))
    ledger.record_fill(fill, "BUY", canary_experiment_id="exp-1",
                       canary_baseline_quantity=10)
    assert ledger.list_canary_order_rows("exp-1") == [{
        "id": "buy-1", "ticker": "AAPL", "side": "BUY", "quantity": 5,
        "fill_price": 100.0, "fees": 1.0,
        "filled_at": fill.filled_at.isoformat(),
        "canary_baseline_quantity": 10,
    }]
```

- [ ] **Step 2: Run the tests and verify the missing keyword/query failure**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_durable_orders.py -q`
Expected: FAIL because the record keywords and query method do not exist.

- [ ] **Step 3: Add nullable columns, parameters, and chronological query**

Add idempotent `ALTER TABLE` migrations for `canary_experiment_id TEXT` and
`canary_baseline_quantity INTEGER`. Extend the existing INSERT and return rows
ordered by `filled_at ASC, id ASC` from `list_canary_order_rows`.

- [ ] **Step 4: Run ledger tests**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_durable_orders.py tests/test_paper_broker.py tests/test_ledger_locks.py -q`
Expected: PASS.

### Task 2: Idempotent Paired Shadow Accounting

**Files:**
- Modify: `trading_bot/learning/experiments/shadow.py:34-385`
- Create: `tests/test_runtime_canary_idempotency.py`

**Interfaces:**
- Consumes: durable `order_id`, side, quantities, price, and fees.
- Produces: `PairedShadowHarness.record_entry(operation_id: str, ...) -> None`
- Produces: `PairedShadowHarness.record_exit(operation_id: str, ...) -> None`
- Produces: `candidate_completed_trades()`, `baseline_completed_trades()`, and `completed_trade_counts_match()`.

- [ ] **Step 1: Write failing duplicate and partial-exit tests**

```python
def test_duplicate_operation_is_applied_once(harness):
    harness.record_entry(operation_id="buy-1", ticker="AAPL",
                         baseline_quantity=10, candidate_quantity=5,
                         fill_price=100.0, fees=1.0)
    harness.record_entry(operation_id="buy-1", ticker="AAPL",
                         baseline_quantity=10, candidate_quantity=5,
                         fill_price=100.0, fees=1.0)
    assert harness.candidate.snapshot_positions()["AAPL"]["qty"] == 5

def test_partial_exit_does_not_complete_trade(harness):
    harness.record_entry(operation_id="buy-1", ticker="AAPL",
                         baseline_quantity=10, candidate_quantity=5,
                         fill_price=100.0, fees=1.0)
    harness.record_exit(operation_id="sell-1", ticker="AAPL",
                        candidate_quantity=2, fill_price=105.0, fees=1.0)
    assert harness.candidate_completed_trades() == 0
```

- [ ] **Step 2: Run tests and verify signature/count failures**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_idempotency.py -q`
Expected: FAIL because operation IDs and completed-position counts are absent.

- [ ] **Step 3: Implement operation replay and completed-position accounting**

Persist `operation_id` in each `ShadowFill`. Maintain an applied-ID set while
recording and replaying. Increment completed positions only when a SELL changes
a positive ticker quantity to zero. Preserve each realized partial P&L in
metrics while exposing completion counts separately.

- [ ] **Step 4: Run shadow suites**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_idempotency.py tests/test_runtime_canary_harness.py tests/test_paired_shadow_lifecycle.py -q`
Expected: PASS after existing fixtures supply operation IDs.

### Task 3: Production Lifecycle and Reconciliation

**Files:**
- Modify: `trading_bot/learning/experiments/runtime_canary.py:44-237`
- Create: `tests/test_runtime_canary_lifecycle.py`

**Interfaces:**
- Produces: `begin_runtime_canary(settings: Settings, ledger: PortfolioLedger) -> RuntimeCanaryContext | None`
- Produces: `finish_runtime_canary(context: RuntimeCanaryContext | None) -> None`
- Retains an explicit test-only construction seam through optional private helpers, not the production API.
- Consumes: `PortfolioLedger.list_canary_order_rows()` and paired applied operation IDs.

- [ ] **Step 1: Write failing production-load and reconciliation tests**

```python
def test_begin_derives_canonical_store(settings, ledger, seeded_canary):
    context = begin_runtime_canary(settings, ledger)
    assert context is not None
    assert context.store.root == Path(settings.app.state_db_path).parent / "tuning_experiments"

def test_begin_reconciles_missing_shadow_fill(settings, ledger, seeded_canary):
    ledger.record_fill(make_fill("buy-1"), "BUY",
                       canary_experiment_id=seeded_canary.experiment_id,
                       canary_baseline_quantity=10)
    context = begin_runtime_canary(settings, ledger)
    assert context.harness.candidate.snapshot_positions()["AAPL"]["qty"] == 5
```

- [ ] **Step 2: Run tests and verify missing lifecycle API**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_lifecycle.py -q`
Expected: FAIL because the central lifecycle functions do not exist.

- [ ] **Step 3: Implement canonical construction, ordered reconciliation, and finish**

Derive `<state-db-parent>/tuning_experiments`, build the controller and harness,
use persisted starting equity, replay missing durable rows by order ID, then
snapshot candidate/shadow metrics and completed-position counts in `finish`.
Differentiate inactive state from malformed/inaccessible active state through
logging and raised lifecycle errors.

- [ ] **Step 4: Run lifecycle and context tests**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_lifecycle.py tests/test_runtime_canary_context.py tests/test_runtime_canary_cli.py -q`
Expected: PASS.

### Task 4: Fail-Closed Activation and Unified Terminal Finalization

**Files:**
- Modify: `trading_bot/learning/experiments/controller.py:183-510`
- Create: `tests/test_runtime_canary_activation.py`

**Interfaces:**
- Produces: `ExperimentController.finalize_terminal(state, status, reason=None) -> ExperimentState`
- Activation persists `canary_starting_equity` before activating candidate bytes.
- Runtime invalidation calls terminal finalization immediately.

- [ ] **Step 1: Write failing activation and invalidation tests**

```python
def test_activation_persists_equity_before_candidate_write(controller, store, ledger, spy):
    controller.evaluate()
    assert spy.events.index("starting_equity_saved") < spy.events.index("candidate_activated")

def test_runtime_failure_restores_and_archives(context, overrides):
    context.invalidate("snapshot_failure")
    assert overrides.read_bytes() == context.store.baseline_bytes(context.state.experiment_id)
    assert not context.store.current_path.exists()
```

- [ ] **Step 2: Run tests and verify current fail-open/early-return behavior**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_activation.py -q`
Expected: FAIL because activation does not persist starting equity and invalidation does not finalize.

- [ ] **Step 3: Implement one terminal path and fail-closed activation**

Replace exception-as-flat behavior with explicit initialization only for a
missing ledger and terminal `ERROR` for unreadable storage. Route unsupported,
non-flat, invalidated, rollback, timeout, and decision outcomes through one
finalizer that restores baseline where required, persists state, logs, and
archives. If restoration fails, retain active `ERROR` state and raise a
dedicated exception.

- [ ] **Step 4: Run controller suites**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_activation.py tests/test_runtime_canary_controller.py tests/test_paired_canary_gates.py tests/test_experiment_activation_rollback.py -q`
Expected: PASS.

### Task 5: Durable Execution Ordering

**Files:**
- Modify: `trading_bot/runtime/orchestrator.py:875-1064`
- Modify: `trading_bot/runtime/position_exit.py:18-153`
- Modify: `trading_bot/runtime/fill_transaction.py`
- Test: `tests/test_runtime_canary_buy_execution.py`
- Test: `tests/test_runtime_canary_sell_execution.py`

**Interfaces:**
- Consumes: active context experiment ID and baseline BUY quantity.
- Durable ledger callbacks receive canary metadata.
- Context notifications receive `operation_id=fill.order_id` only after durable fill persistence succeeds.

- [ ] **Step 1: Add failing ordering and metadata tests**

```python
def test_failed_buy_transaction_does_not_record_shadow(...):
    transaction_persist.side_effect = RuntimeError("db failed")
    result = run_paper_trade(..., runtime_canary=context)
    assert context.harness.entries == []

def test_sell_row_contains_active_canary_id(...):
    fill_sell_position(..., runtime_canary=context)
    assert ledger.list_canary_order_rows(context.state.experiment_id)[0]["side"] == "SELL"
```

- [ ] **Step 2: Run focused execution tests and verify failures**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_buy_execution.py tests/test_runtime_canary_sell_execution.py -q`
Expected: FAIL because BUY notification precedes transaction success and rows lack metadata.

- [ ] **Step 3: Move notifications and persist metadata atomically with order rows**

Pass canary metadata into the ledger callback, run all durable callbacks, then
notify the context using `fill.order_id`. Apply the same order to SELLs. Do not
record dry-run or rejected orders.

- [ ] **Step 4: Run execution regression tests**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_buy_execution.py tests/test_runtime_canary_sell_execution.py tests/test_fill_transaction.py tests/test_buy_fill_transaction.py tests/test_fill_persistence_fail_closed.py tests/test_run_paper_trade_uses_candidate.py -q`
Expected: PASS.

### Task 6: CLI and Continuous Runtime Boundaries

**Files:**
- Modify: `trading_bot/cli/app.py:358-399,745-1016`
- Modify: `trading_bot/runtime/continuous_loop.py:530-644`
- Test: `tests/test_runtime_canary_cli.py`
- Test: `tests/test_continuous_loop_canary_and_trailing.py`

**Interfaces:**
- Consumes: `begin_runtime_canary(settings, ledger)` and `finish_runtime_canary(context)`.
- Every command/cycle calls finish in `finally` so early returns and exceptions cannot skip snapshots or rollback handling.

- [ ] **Step 1: Write failing begin/finish symmetry tests**

```python
def test_continuous_cycle_begins_and_finishes_once(monkeypatch):
    begin = MagicMock(return_value=context)
    finish = MagicMock()
    monkeypatch.setattr(continuous_loop, "begin_runtime_canary", begin)
    monkeypatch.setattr(continuous_loop, "finish_runtime_canary", finish)
    run_continuous_loop(..., max_cycles=1)
    begin.assert_called_once()
    finish.assert_called_once_with(context)
```

- [ ] **Step 2: Run boundary tests and verify missing finish calls**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_cli.py tests/test_continuous_loop_canary_and_trailing.py -q`
Expected: FAIL because production uses the old loader and continuous mode never snapshots.

- [ ] **Step 3: Wire lifecycle in `try/finally` boundaries**

Replace direct loader calls and ad hoc snapshots. Ensure each continuous-loop
retry finalizes the current context before starting another cycle. Propagate
terminal restoration failures into the existing command/cycle failure path.

- [ ] **Step 4: Run CLI and continuous regression tests**

Run: `.venv/bin/python -m pytest tests/test_runtime_canary_cli.py tests/test_continuous_loop_canary_and_trailing.py tests/test_cli_smoke.py -q`
Expected: PASS.

### Task 7: Documentation and End-to-End Verification

**Files:**
- Modify: `AGENTS.md`
- Modify: `ARCHITECTURE.md`
- Test: `tests/test_runtime_canary_end_to_end.py`

**Interfaces:**
- Documents the exact production lifecycle, durable metadata, completed-position gate, and immediate rollback behavior.

- [ ] **Step 1: Update the end-to-end test to use production lifecycle APIs**

Drive proposal acceptance, activation, 20 complete paired positions, cycle
finalization, and deterministic keep/rollback without injecting a controller
into runtime loading.

- [ ] **Step 2: Update operational documentation**

Replace the old optional-context description with canonical store derivation,
begin/finish boundaries, durable order reconciliation, completed-position
counting, fail-closed activation, and immediate terminal rollback.

- [ ] **Step 3: Run the focused runtime-canary suite**

Run: `.venv/bin/python -m pytest -q tests/test_runtime_canary_*.py tests/test_paired_shadow_lifecycle.py tests/test_paired_canary_gates.py tests/test_continuous_loop_canary_and_trailing.py`
Expected: all selected tests pass with zero failures.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: zero new failures; report any documented pre-existing failure separately.

- [ ] **Step 5: Inspect final changes**

Run: `git status --short && git diff --check && git diff --stat`
Expected: only intended runtime-canary implementation, tests, and documentation; no whitespace errors.
