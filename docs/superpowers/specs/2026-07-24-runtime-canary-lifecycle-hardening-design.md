# Runtime Canary Lifecycle Hardening Design

**Date:** 2026-07-24
**Status:** Approved

## Purpose

Make the runtime canary a real production contract rather than optional
plumbing. A supported tuning candidate must collect paired runtime evidence,
survive restarts without changing its accounting baseline, and restore the
baseline immediately when tracking becomes unreliable.

The live paper candidate remains the only order submitted to `PaperBroker`.
The baseline remains an isolated shadow ledger that receives the same fill
prices and exit timing at independently derived quantities.

## Current Problems

The existing implementation has several lifecycle gaps:

- Production callers invoke `load_runtime_canary(settings, ledger)` without a
  controller or store, causing the loader to return `None` even during an
  active canary.
- The continuous loop does not snapshot canary metrics.
- `activate_canary()` is not integrated into activation, so immutable starting
  equity is not persisted.
- Runtime invalidation writes `INCONCLUSIVE`, but `evaluate()` returns before
  restoring baseline overrides or archiving the experiment.
- Ledger read failures are treated as a flat portfolio and can authorize
  activation.
- Shadow BUY recording occurs before the durable BUY transaction succeeds.
- Partial SELL fills count as completed canary trades.
- Runtime load failures are silently indistinguishable from no active canary.

## Scope

This change will:

- Centralize production runtime-canary construction and finalization.
- Make activation atomic and fail closed.
- Persist immutable starting equity before candidate activation.
- Make runtime fill recording durable, restart-safe, and idempotent.
- Count completed positions rather than individual SELL fills.
- Snapshot metrics at every manual command and continuous-loop boundary.
- Restore and archive immediately when runtime tracking fails.
- Add end-to-end production-path tests.

## Non-Goals

- Supporting tuning parameters other than
  `supermodel.range_bound_trend_caution_multiplier`.
- Submitting baseline orders to the paper broker.
- Moving canary processing into a separate daemon.
- Mutating a process's already-loaded `Settings` object during a cycle.
- Preserving runtime canary artifacts produced by the broken production
  wiring. A new experiment starts with the corrected artifact schema.

## Architecture

### Runtime Lifecycle Boundary

`trading_bot.learning.experiments.runtime_canary` will expose one production
lifecycle boundary with two operations:

```text
begin(settings, ledger) -> RuntimeCanaryContext | None
finish(context) -> finalization result
```

`begin` will derive the canonical experiment root from the configured state
database directory, construct the `ExperimentStore` and
`ExperimentController`, load the active state, validate support, and rebuild
the paired harness from durable artifacts.

Production callers will not pass an optional store or controller. Tests may
use a separate explicit dependency-injection seam so the production API
cannot accidentally omit required dependencies.

`finish` will snapshot paired metrics and persist the market-session and
completed-trade state. If snapshotting fails, it will invoke the same terminal
rollback path used by all other runtime failures.

### Command Boundaries

Each runtime operation owns exactly one context:

- `paper-trade`: begin once, process all symbols, finish once.
- `manage-positions`: begin once, process all positions, finish once.
- Continuous mode: begin once at the start of each cycle, share the context
  across BUY and SELL phases, finish once before the cycle completes.

No active experiment remains a cheap, side-effect-free `None` path.

## Activation State Machine

Activation remains `PROPOSED -> CANARY`, but all required work becomes one
ordered controller transition:

1. Verify candidate checksum and offline acceptance.
2. Verify the change is runtime-canary supported.
3. Open and read the live ledger successfully.
4. Require every live position quantity to be zero.
5. Read current portfolio equity.
6. Persist `canary_starting_equity` and `runtime_canary_armed`.
7. Atomically activate candidate override bytes.
8. Persist `CANARY` and append the activation event.

Failures before step 7 leave baseline overrides untouched. Failures at or
after step 7 restore baseline bytes before returning a terminal outcome.

An absent, never-created ledger may be initialized normally. Corruption,
permission errors, schema errors, and other read failures are not interpreted
as a flat portfolio.

## Runtime Accounting

### Entries

The execution path computes:

- Baseline quantity: risk-approved quantity before the canary policy.
- Candidate quantity: actual filled quantity after the policy.

The live BUY transaction must complete before either shadow ledger records the
entry. The paired entry then records both quantities at the actual candidate
fill price and fixed order fee.

If durable BUY persistence fails, no shadow entry is written.

### Exits

All full and partial exits continue through the shared
`fill_sell_position()` seam after durable SELL persistence.

The baseline exit quantity is proportional to the candidate position:

```text
round(baseline held before * candidate sold / candidate held before)
```

A final candidate exit clears both paired positions. If one side closes while
the other remains open, or completed-position counts diverge, the experiment
is immediately inconclusive and rolled back.

### Completed Trade Definition

`canary_closed_trades` counts completed positions, not SELL fills. A paired
trade advances the decision gate only when the ticker position reaches zero.
Partial exits realize P&L but do not increment the completed-trade count.

Profit factor and net P&L still include each realized partial-exit component.
The completed-position count is a separate decision-gate measure.

### Idempotency

Each shadow operation will use `FillResult.order_id`, which is already stored
as the primary key of the durable `orders` row. Both recording and replay
reject an already-applied order ID.

This prevents duplicate shadow fills when a command retries or a process
restarts after durable persistence but before cycle finalization.

The `orders` table will gain additive nullable canary metadata:

- `canary_experiment_id`
- `canary_baseline_quantity`

BUY rows record the active experiment and pre-policy baseline quantity in the
same SQLite insert as the candidate fill. SELL rows record the active
experiment; their baseline quantity remains derived from the paired position
fraction during ordered replay.

At `begin` and before `finish` snapshots, the lifecycle reconciles durable
orders for the active experiment against applied shadow order IDs in
`filled_at, id` order. Any durable operation missing from JSONL is applied
once. This closes the crash window between the SQLite commit and shadow JSONL
append without requiring a separate daemon or cross-storage transaction.

The artifact schema will include the operation ID and enough information to
rebuild:

- Cash
- Open quantities and cost basis
- Realized P&L components
- Completed-position count
- Applied-operation IDs
- Equity curve

## Metrics

At `finish`, the controller atomically updates:

- `candidate_metrics` from the runtime candidate ledger.
- `shadow_metrics` from the runtime baseline ledger.
- `canary_closed_trades` from completed candidate positions.
- `market_sessions` from observed durable fills.

`baseline_metrics` continues to hold offline replay baseline evidence and is
never overwritten by runtime shadow metrics.

The existing decision thresholds remain unchanged:

- Early review after 10 completed positions.
- Final decision after 20 completed positions.
- Early profit-factor floor of 0.50.
- Candidate profit factor must beat shadow by at least 0.10.
- Candidate net P&L must exceed shadow net P&L.
- Candidate drawdown may exceed shadow by at most 5 percentage points at the
  final gate.
- Fewer than 20 completed positions after 10 market sessions is
  `INCONCLUSIVE`.

## Terminal Finalization

The controller will provide one terminal finalization method used for
`KEPT`, `ROLLED_BACK`, `INCONCLUSIVE`, and `ERROR`. It owns:

1. Baseline restoration when the candidate is not kept.
2. Terminal status and reason persistence.
3. Rollback timestamp persistence where applicable.
4. Event logging.
5. Archival of state and experiment artifacts.

This removes status-specific early returns that currently bypass restoration
or archival.

Unsupported candidates and non-flat activation attempts become archived
`INCONCLUSIVE` experiments, allowing a later proposal to proceed.

## Runtime Failure Policy

Once candidate overrides are active, any entry, exit, artifact, session, or
snapshot tracking failure will:

1. Invalidate the current context and stop further shadow writes.
2. Restore baseline override bytes atomically.
3. Persist the reason and rollback timestamp.
4. Archive the experiment as `INCONCLUSIVE`.
5. Let the current paper command complete without additional canary writes.
6. Use baseline settings on the next command or continuous-loop cycle.

The process will not mutate its current in-memory `Settings` midway through a
command or cycle.

If baseline restoration itself fails:

- Record a critical event and log message.
- Leave the experiment in `ERROR` with its state available for recovery.
- Manual commands return non-zero.
- Continuous mode records a cycle failure and enters its existing failure
  circuit rather than continuing silently.

Malformed active state or inaccessible storage must be observable. It must not
be represented as an inactive canary.

## Persistence

The canonical experiment root remains:

```text
<state-db-parent>/tuning_experiments/
```

It contains:

```text
current.json
events.jsonl
<experiment-id>/
  baseline.yaml or baseline.absent
  candidate.yaml
  shadow-fills.jsonl
  shadow-equity.jsonl
  candidate-shadow-fills.jsonl
  candidate-shadow-equity.jsonl
archived/<experiment-id>/
  current.json
  files/
```

Atomic replacement remains required for experiment state and override files.
JSONL artifacts remain append-only and replayable.

## Observability

The lifecycle will distinguish these outcomes in logs and events:

- No active experiment.
- Active and loaded.
- Unsupported active change.
- Activation gate rejected.
- Runtime tracking invalidated.
- Snapshot persisted.
- Baseline restored.
- Baseline restoration failed.
- Experiment archived.

`tune-experiment status` will continue to expose active metrics. Evaluation
output must report the terminal result returned by `evaluate()` even after
the active state has been moved to the archive.

## Testing Strategy

Tests will remain deterministic and network-free.

### Lifecycle Tests

- A real production `begin(settings, ledger)` derives and loads the canonical
  store without injected dependencies.
- No active experiment returns `None` without artifact writes.
- Malformed or inaccessible active state produces an observable failure.
- Manual paper and manage commands call begin and finish exactly once.
- Continuous mode calls begin and finish exactly once per cycle.

### Activation Tests

- Starting equity is persisted before candidate bytes are activated.
- Non-flat portfolios do not activate candidate bytes.
- Ledger read failures fail closed.
- Missing candidate snapshots restore baseline and terminate.
- Unsupported candidates are archived as inconclusive.

### Accounting Tests

- Shadow BUY recording occurs only after durable BUY success.
- Baseline and candidate quantities are recorded exactly once.
- Durable canary metadata is written in the same order-row insert as the fill.
- Reconciliation backfills a durable fill missing from JSONL after a simulated
  process crash.
- Partial exits preserve proportional quantities without advancing the
  completed-position gate.
- Final exits increment one completed position on both sides.
- Divergent paired completion triggers immediate rollback.
- Reusing an operation ID is idempotent before and after restart.

### Failure Tests

- Entry, exit, session, and snapshot failures immediately restore baseline and
  archive the experiment.
- Baseline restoration failure is surfaced as critical and non-zero.
- The current command may finish with its loaded settings, while the next
  lifecycle starts from baseline settings.

### Decision Tests

- Continuous cycles persist metrics and completed-position counts.
- Ten-session timeout uses completed positions.
- End-to-end activation through 20 completed positions reaches deterministic
  keep or rollback outcomes.
- Offline baseline metrics remain unchanged by runtime snapshots.

### Regression Tests

- Existing no-canary execution remains side-effect-free.
- Existing kill switch, paper-only enforcement, exit priority, fill
  persistence, and tuning override drift protection remain intact.

## Acceptance Criteria

The work is complete when:

1. Every production paper-trading path can load an active canary from the
   canonical store without dependency injection.
2. Candidate activation cannot occur without supported parameters, a readable
   flat portfolio, and persisted starting equity.
3. Every durable candidate fill is represented exactly once in both paired
   ledgers after lifecycle reconciliation.
4. Every manual command and continuous cycle persists current runtime metrics.
5. The 20-trade gate counts completed positions, not partial SELL fills.
6. Any runtime tracking failure restores baseline immediately and archives the
   experiment, unless restoration itself fails and is reported as critical.
7. Restarted processes reconstruct identical paired state and metrics from the
   same immutable starting equity and artifacts.
8. Focused runtime-canary tests and the full network-free test suite pass.
9. `AGENTS.md` and `ARCHITECTURE.md` are updated in the implementation commit
   to describe the corrected operational contract.
