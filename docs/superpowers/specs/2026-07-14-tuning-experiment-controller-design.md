# Tuning Experiment Controller Design

**Date:** 2026-07-14
**Status:** Approved, awaiting implementation
**Owner:** Burn-in loop
**Target release:** Next minor

## Goal

Replace the existing `trading_bot tune` heuristic with a controller that proves a proposed tuning change improves profit factor before letting it stay in production, and that automatically rolls the change back if it underperforms on real paper trades.

Concretely: when `trading_bot tune` says "loosen `counter_veto_weight` from 1.0 to 0.75," the experiment controller must show, on the same data and then on the same market regime, that this candidate earns a higher profit factor than the frozen baseline. If not, the controller rolls the override back.

## Background

The graduation cohort has 62 of 100 closed trades with PF `0.74` and net `- $533`. Both active strategies lose: `v3-trend_following` PF `0.76`, `v3-mean_reversion` PF `0.68`. The current `tune` command reacts to rejection rate and recent win rate but does not:

- Constrain changes to one allowlisted parameter at a time
- Validate that the proposed change actually improves profit factor
- Compare candidate and baseline inside the same market regime
- Roll back automatically if the change underperforms in production

This spec closes those gaps with a narrow experiment controller around the existing tuner.

## Design

### Layered structure

The controller sits around the existing tuner and the burn-in nightly hook:

```text
proposal -> offline validation -> paired canary -> decision
            (causal replay)       (shadow baseline)
```

Each stage gates the next. The candidate only reaches real `PaperBroker` when both stages succeed.

### Allowlist

Only four fields are ever tunable:

| Section | Field | Default | Step |
|---|---|---|---|
| `supermodel` | `support_threshold` | 0.72 | ±0.05 |
| `supermodel` | `block_threshold` | 0.30 | ±0.05 |
| `supermodel` | `counter_veto_weight` | 1.00 | ±0.25 |
| `strategy_tracker` | `full_allocation_rate` | 0.50 | ±0.05 |

Risk limits, stops, daily-order limits, allocation ceilings, the 50-share cap, and any non-allowlisted field are never writable by the controller.

### Proposal selection

When the tuner proposes more than one change, the controller picks exactly one based on a fixed priority order: `counter_veto_weight` → `block_threshold` → `support_threshold` → `full_allocation_rate`. Each change is bounded by its step and clamped to `[0.0, 1.0]`. No change returns `None` and no experiment starts.

### Offline validation

The controller runs baseline and candidate through the same causal backtest on local EOD data. Train/validation split is chronological 70/30.

The candidate is accepted only if the validation segment satisfies all of:

- At least 20 candidate closed trades
- Candidate PF ≥ baseline PF + `0.10`
- Candidate net P&L > baseline
- Candidate max drawdown no more than 5 percentage points worse
- Candidate trade count at least 80% of baseline

When any condition fails, the experiment is recorded as `OFFLINE_REJECTED` and stays offline. Baseline settings remain untouched.

### Paired canary

When offline validation passes, the candidate is promoted to a live paper canary. During the canary:

- The candidate controls real paper orders.
- The frozen baseline runs against the same completed prior-day context, intraday and hourly frames.
- Baseline signals reach an isolated shadow paper broker with identical fees and slippage.
- Baseline decisions are persisted in the experiment artifact directory, never in `state/burn_in.db`.
- Portfolio-wide correlation and sector checks remain a documented limitation of shadow parity. The actual candidate still passes every live paper-runtime risk control.

### Decision rules

Evaluate after 20 candidate closed trades. Keep the change only if all of the following hold:

- Candidate PF ≥ shadow baseline PF + `0.10`
- Candidate net P&L > shadow baseline
- Candidate drawdown no more than 5 percentage points worse than shadow

Otherwise roll back to the exact baseline overrides.

Apply early rollback after 10 closed trades if:

- Candidate PF < `0.50`, or
- Candidate drawdown exceeds shadow baseline by more than 10 percentage points

Apply timeout rollback if fewer than 20 candidate closed trades arrive within 10 market sessions. Mark the experiment `INCONCLUSIVE` and restore baseline.

### Persistence

State lives under `state/tuning_experiments/`:

```text
state/tuning_experiments/
├── current.json           # exactly one ExperimentState (atomic write)
├── events.jsonl           # append-only audit log
└── <experiment-id>/
    ├── baseline.yaml      # frozen baseline overrides
    ├── candidate.yaml     # active candidate overrides
    ├── offline-result.json
    └── canary-result.json
```

`current.json` is replaced atomically via temp-file-plus-rename. `events.jsonl` is append-only and records every transition: proposed, offline_rejected, canary_started, kept, rolled_back, inconclusive, and error.

Each experiment has an ID built from a UTC timestamp plus a short fingerprint of the candidate field and value, e.g. `2026-07-14T13:42:17Z__counter_veto_weight-1.00-to-0.75`.

### Failure behavior

- Corrupt or missing active state is treated as `ERROR`. The controller records the reason and restores baseline overrides.
- All exception paths log the reason and never break the burner.
- Before canary, any failure leaves baseline untouched.
- During canary, any failure that prevents further evaluation triggers baseline restoration.

### Burn-in integration

The nightly step in `scripts/auto-burn-in.sh` becomes:

1. Check experiment status. If `CANARY`, run `tune-experiment evaluate`.
2. If no active experiment, run `tune-experiment propose`.
3. The controller decides whether to promote to canary, reject, or stop without action.
4. The existing `tune` command remains for manual exploration.

`./tradebot-local tune` no longer writes `state/tuning_overrides.yaml` directly when an experiment is active; it prints a notice and exits non-zero.

### CLI surface

```bash
./tradebot-local tune-experiment propose
./tradebot-local tune-experiment status
./tradebot-local tune-experiment evaluate
./tradebot-local tune-experiment rollback [--reason "operator note"]
./tradebot-local tune-experiment status --json
```

Exit codes: `0` success, `1` transient failure (with baseline restored), `2` operator-visible error (corrupt state).

### Health integration

`./tradebot-local doctor --burn-in` gains a `tuning_experiment` row:

- `PASS` when no experiment is active or the canary is making forward progress
- `WARN` when the canary is stale (>2 hours without a new closed trade during market hours) or `INCONCLUSIVE`
- `FAIL` when active state is corrupt, when `ERROR` is recorded, or when rollback failed

### Operator documentation

`AGENTS.md` gains a "Tuning Experiment Controller" section documenting:

- Commands and exit codes
- The four allowlisted parameters
- The validation and canary gates
- The state directory and audit log
- How to manually roll back

## Constraints

- `live_trading_enabled` stays forced `False`.
- Paper-only enforcement and Robinhood MCP-only integration are unchanged.
- All writes use atomic replacement.
- All tests are network-free and deterministic.
- Risk limits, stops, allocation ceilings, daily-order limits, and the 50-share cap cannot be tuned.

## Out of scope

- Dashboard UI for experiment status
- Multi-parameter search
- Auto-tuning of risk or session knobs
- Cross-experiment transfer learning

## Verification

- Existing `tests/test_tuning_overrides.py` continues to pass.
- New `tests/test_tuning_experiment_store.py`, `tests/test_tuning_experiment_proposal.py`, `tests/test_tuning_experiment_replay.py`, `tests/test_tuning_experiment_shadow.py`, `tests/test_tuning_experiment_controller.py`, `tests/test_tuning_experiment_cli.py`, `tests/test_tuning_experiment_health.py` all pass.
- `tests/test_auto_burn_in_script.py` is extended for `tune-experiment` wiring.
- Full `pytest -q` remains green.
- `bash -n scripts/auto-burn-in.sh` passes.
- Manual smoke: `tune-experiment propose` with mocked EOD bars produces a deterministic offline result; `tune-experiment status` renders correctly; `tune-experiment rollback` writes the exact baseline bytes.