# Paired Shadow Harness Runtime Wiring — Design

## Status

> **Status: Historical implementation design — not current operational authority.** Approved by user 2026-07-21 (Revision 2). The runtime canary wiring it described has now landed across commits `fd591ee` (harness, controller, context, persistence, runtime canary tests), `e79e0ff` (BUY/SELL/CLI/continuous_loop wiring), and `1db904f` (AGENTS.md contract). Current behavior is defined by `AGENTS.md` lines 291-335, the tracked implementation, and the runtime-canary tests. Some details below differ from the implemented value-range contract (baseline/candidate both accepted in `(0, 1]`) and the artifact filenames; refer to the code for current truth.

## Context

The `PairedShadowHarness` class in `trading_bot/learning/experiments/shadow.py`
exists, is fully implemented, and has its own test file
(`tests/test_tuning_experiment_shadow.py`). It supports baseline-vs-candidate
ledger mirroring.

However, the harness is dead code at the runtime level:

1. `run_paper_trade()` always passes `shadow=None`.
2. `_maybe_record_shadow_fill()` only mirrors BUY fills. SELL exits
   (`position_exit.py`, `continuous_loop.py`) are never mirrored.
3. The CLI never constructs a `PairedShadowHarness` when an experiment is
   in `CANARY`.
4. `ExperimentController.evaluate()` advances experiments to `CANARY`
   but has no way to plumb a shadow harness back into the next bot process.

Net effect: experiments leave `OFFLINE_REJECTED`, but the
`CANARY → KEPT/ROLLED_BACK` decision never fires because no live data
flows into the baseline/candidate ledgers.

## Audit Corrections (Revision 2)

This revision corrects five flaws found in the original approved draft:

1. **Manager duplication.** `cli/app.py._run_manage_positions_once` is a
   separate implementation of position management from
   `runtime/continuous_loop.py._run_manage_positions_once`. Wiring only
   the runtime copy would miss SELLs emitted by the CLI on the burner.
   Fix: instrument the **shared `position_exit.py` seam** so all exit
   paths are covered exactly once.
2. **Double-applied multiplier.** The runtime BUY already scales by
   `policy_multiplier`. Re-applying it inside the harness would shrink
   the candidate twice. Fix: the harness records exact pre-policy and
   post-policy quantities; it never multiplies again.
3. **Hard-coded `candidate_multiplier`.** `PairedShadowHarness` currently
   applies a fixed multiplier to every fill. Only `V3 + trend-following
   + range-bound + caution` trades are scaled. Fix: callers supply
   exact baseline and candidate quantities per fill.
4. **Restart cash poisoning.** Seeding restart cash from `live ledger`
   replays prior fills against a different starting value. Fix: persist
   an immutable `canary_starting_equity` at activation; the harness
   reads it on every reload.
5. **Unsupported canary parameters.** The harness can only faithfully
   canary sizing-only policy changes. Threshold changes that alter
   signal selection cannot be simulated safely. Fix: gate runtime-canary
   support on a allowlist of `(section, field)` pairs and a numeric
   range. Anything else forces `INCONCLUSIVE` and baseline restoration.

## Goals

1. Mirror every live BUY and SELL into a paired (baseline, candidate)
   ledger while an experiment is in `CANARY`.
2. Persist canary metrics at the decision boundary so `evaluate()` can
   advance `CANARY → KEPT/ROLLED_BACK` based on real trade data.
3. Preserve existing semantics when no experiment is active: zero shadow
   work, zero behaviour change.
4. Be testable without network, as required by AGENTS.md.
5. Survive restart by rebuilding the harness from JSONL.
6. Support a single canary parameter today:
   `supermodel.range_bound_trend_caution_multiplier` with
   `0 < candidate < 1`.

## Non-Goals

- Backfilling historical paper trades into the shadow ledgers.
- Modifying offline replay; it remains independent.
- Supporting any canary parameter other than the one above. Adding
  more requires its own design revision and an exhaustive fixture.
- Touching the broker or ledger code paths.

## Architecture

### Single source of truth: the active experiment

`ExperimentController` is the only authority. A `RuntimeCanaryContext`
holds the controller, the loaded `ExperimentState`, the JSONL store
for the harness, and the harness itself. The context is `None` unless
`state.status == "CANARY"` and the change parameter is on the allowlist.

### Quantity contract (the corrected core)

Every BUY executor that wants to mirror a fill calls:

```python
runtime_canary.record_entry(
    ticker=fill.ticker,
    baseline_quantity=decision.position_size,         # pre-policy
    candidate_quantity=fill.quantity,                  # actual fill, post-policy
    fill_price=fill.fill_price,
    fees=fill.fees,
)
```

The harness records both positions at the supplied quantities; it
**never applies any multiplier**. When a candidate SELL happens, the
harness derives a baseline SELL quantity from the fraction of the
candidate position sold:

```
candidate_held_before = candidate_tracker[ticker]
candidate_sell_qty = fill.quantity
fraction = candidate_sell_qty / candidate_held_before
baseline_held_before = baseline_tracker[ticker]
baseline_sell_qty = round(baseline_held_before * fraction)
```

A final exit closes both ledgers fully. Pre-canary positions are never
mirrored, so a SELL for a position opened before `state.started_at`
is skipped.

### SELL seam

Every full and partial SELL calls into the shared
`trading_bot/runtime/position_exit.py::fill_sell_position(..., runtime_canary=None)`.
After the real broker fill succeeds and the ledger is updated, the
seam forwards the call to `runtime_canary.record_exit`. The CLI-local
manager, the runtime continuous manager, and any future caller all
hit this one place, so wiring it once covers every exit path.

### Restricted parameter support

```
Runtime canary support predicate accepts only:
  ("supermodel", "range_bound_trend_caution_multiplier")

Candidate value must satisfy 0 < candidate < 1.
Baseline value must be exactly 1.0 (current production default).
```

Anything else triggers the INCONCLUSIVE path: the harness never
constructs, the controller restores baseline overrides if needed,
and the experiment is archived.

### Flat-portfolio activation

After the offline gate accepts a candidate, the controller cannot
activate a runtime canary until the live portfolio is flat
(`sum(position.quantity for position in state.positions.values()) == 0`).
If the live portfolio is not flat, the experiment transitions to
`INCONCLUSIVE` with reason `non_flat_portfolio_on_canary_start`.

### Restart resilience

`ExperimentState.canary_starting_equity` is persisted **once** at the
moment the canary begins. The harness reads this value on every
reload. The harness JSONL (`baseline-fills.jsonl`,
`candidate-fills.jsonl`, `shadow-equity.jsonl`) is the audit trail.
Appending one line at a time is atomic on POSIX when the line is under
`PIPE_BUF` (4 KB) — each fill payload is ~200 bytes, well within
bounds.

### Decision-boundary metrics

`ExperimentController.evaluate()` reads `state.candidate_metrics` and
`state.shadow_metrics`. Naming convention is:

- `state.baseline_metrics` — offline replay baseline (unchanged).
- `state.candidate_metrics` — runtime candidate metrics.
- `state.shadow_metrics` — runtime baseline metrics (the candidate's
  paired shadow).

The CLI snapshots these at the end of every paper-trade and
manage-positions cycle while the canary is in flight. If the candidate
ledger's `closed_trades` differs from the baseline ledger's
`closed_trades`, the canary is marked `INCONCLUSIVE`
(`paired_ledgers_diverged`).

## File-Level Changes

### `trading_bot/learning/experiments/shadow.py`

Refactor `PairedShadowHarness` around exact quantities:

```python
class PairedShadowHarness:
    def record_entry(
        self,
        *,
        ticker: str,
        baseline_quantity: int,
        candidate_quantity: int,
        fill_price: float,
        fees: float,
    ) -> None: ...

    def record_exit(
        self,
        *,
        ticker: str,
        candidate_quantity: int,
        fill_price: float,
        fees: float,
    ) -> None: ...

    def candidate_metrics(self) -> MetricSet: ...
    def baseline_metrics(self) -> MetricSet: ...
    def closed_trade_counts_match(self) -> bool: ...
```

Remove the legacy `record_paired()` and `applied_multiplier` machinery;
they encourage the very bugs this revision is preventing.

### `trading_bot/learning/experiments/models.py`

Add:

```python
class ExperimentState(BaseModel):
    ...
    canary_starting_equity: float | None = None
```

### `trading_bot/learning/experiments/controller.py`

- New method `supports_runtime_canary(change: ParameterChange) -> bool`
  enforcing the allowlist and value range.
- New method `activate_canary(state, ledger) -> None` that:
    1. Verifies flat portfolio.
    2. Records `state.canary_starting_equity = ledger.ensure_portfolio_state().equity`.
    3. Persists the state.
- `evaluate()` rejects unsupported changes with `INCONCLUSIVE` before
  constructing any harness.
- New method `record_canary_snapshot(state, harness)` that:
    1. Sets `state.candidate_metrics = harness.candidate_metrics()`.
    2. Sets `state.shadow_metrics = harness.baseline_metrics()`.
    3. Sets `state.canary_closed_trades =
       state.candidate_metrics.trades`.
    4. Sets `state.baseline_metrics = current offline baseline
       (unchanged)`.
    5. If `not harness.closed_trade_counts_match()`, transitions to
       `INCONCLUSIVE` with reason `paired_ledgers_diverged` and
       restores baseline.

### `trading_bot/learning/experiments/runtime_canary.py` (new)

```python
@dataclass
class RuntimeCanaryContext:
    state: ExperimentState
    controller: ExperimentController
    harness: PairedShadowHarness
    artifacts_dir: Path

    def record_entry(self, *, ticker, baseline_quantity,
                     candidate_quantity, fill_price, fees) -> None: ...

    def record_exit(self, *, ticker, candidate_quantity,
                    fill_price, fees) -> None: ...

    def snapshot(self) -> None: ...
    def invalidate(self, reason: str) -> None: ...


def load_runtime_canary(
    settings: Settings,
    ledger: PortfolioLedger,
) -> RuntimeCanaryContext | None: ...
```

`load_runtime_canary` returns `None` when:

- No experiment exists, or
- Experiment status is not `CANARY`, or
- `supports_runtime_canary(state.change)` returns `False`
  (which then triggers `INCONCLUSIVE` via the controller).

Shadow persistence failures are caught, logged, and the canary is
marked `INCONCLUSIVE` so the burner can keep trading while the
controller archives the experiment and restores baseline.

### `trading_bot/runtime/orchestrator.py`

```python
def run_paper_trade(
    symbols: list[str],
    settings: Settings,
    dry_run: bool = False,
    *,
    runtime_canary: RuntimeCanaryContext | None = None,
) -> list[str]:
```

At the BUY fill site:

```python
baseline_quantity = decision.position_size

if policy_decision.applied and policy_decision.multiplier > 0:
    decision.position_size = max(
        1, int(baseline_quantity * policy_decision.multiplier)
    )

candidate_quantity = fill.quantity

if runtime_canary is not None and fill is not None and not dry_run:
    runtime_canary.record_entry(
        ticker=fill.ticker,
        baseline_quantity=baseline_quantity,
        candidate_quantity=candidate_quantity,
        fill_price=fill.fill_price,
        fees=fill.fees,
    )
```

### `trading_bot/runtime/position_exit.py`

```python
def fill_sell_position(..., runtime_canary: RuntimeCanaryContext | None = None) -> tuple[...]:
    ...
    runtime_canary.record_exit(
        ticker=fill.ticker,
        candidate_quantity=fill.quantity,
        fill_price=fill.fill_price,
        fees=fill.fees,
    )
    ...


def fill_partial_take_profit_position(..., runtime_canary=None) -> tuple[...]:
    ...forward runtime_canary to fill_sell_position...
```

The partial helper already calls `fill_sell_position`, so adding the
parameter once covers both paths.

### `trading_bot/runtime/continuous_loop.py`

```python
def _run_manage_positions_once(
    settings: Settings,
    ledger: PortfolioLedger,
    *,
    runtime_canary: RuntimeCanaryContext | None = None,
) -> dict:
    ...
```

All exit-priority branches forward `runtime_canary` to
`fill_sell_position` / `fill_partial_take_profit_position`.

### `trading_bot/cli/app.py`

- `paper-trade` command: call
  `runtime_canary = load_runtime_canary(ctx.obj, ledger)` once;
  pass it through `run_paper_trade`; at the end, call
  `runtime_canary.snapshot()` if not `None`.
- `manage-positions` command: same pattern, passing the context into
  every fill call.
- `continuous` command: load once per cycle, share across
  scan → trade → manage phases, snapshot once.

## Behaviour Contract

When `runtime_canary is None`:
- Behaviour is byte-identical to today. No shadow disk I/O. No
  harness construction.

When `runtime_canary is not None`:
- BUY fills mirror at the supplied baseline and candidate quantities.
- SELL fills derive baseline quantity from the candidate-side fraction
  sold; the seam fires after the real fill succeeds.
- Fills for positions opened before the canary are not mirrored.
- `harness.candidate_metrics()` and `harness.baseline_metrics()`
  return current closed-trade PF, net P&L, max drawdown, and trade count.
- If `closed_trade_counts_match()` returns `False`, the controller
  forces `INCONCLUSIVE`.
- A second-canary scenario (one experiment in CANARY, another
  proposed) is unsupported: load_runtime_canary returns `None` and
  logs a warning.

## Out of Scope (Logged Follow-Ups)

- Cash reconciliation between shadow and live ledgers if a fill is
  missed (e.g. process crash between broker fill and shadow append).
- Concurrent multi-experiment canaries.
- Per-strategy attribution in the harness.

## Verification Plan

- Harness tests cover entry, partial exit, full exit, restart, and
  trade-count parity.
- Controller tests cover allowlist rejection, flat-portfolio
  rejection, and INCONCLUSIVE-on-divergence.
- BUY-execution tests cover matching policy, nonmatching policy,
  dry-run, and rejected fills.
- SELL-execution tests cover every exit priority (stop, target,
  EOD, time, counter-thesis, trailing) and partial profits.
- CLI lifecycle tests cover `paper-trade`, `manage-positions`, and
  `continuous` (one context per cycle).
- End-to-end test progresses to 20 closed trades, snapshots both
  ledgers, and runs `ExperimentController.evaluate()` to assert a
  deterministic `KEPT` or `ROLLED_BACK` outcome.
- `pytest -q` and a clean-HEAD archive smoke test are run before any
  commit.
