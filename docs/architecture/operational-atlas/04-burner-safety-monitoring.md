# Phase 4 — Burner, Safety, and Monitoring

> 2026-07-29 snapshot at HEAD `62d178b`. Verified live against burner
> PID 89523 at `.burnin_pin/62d178b.../` with all eight health checks
> returning PASS.

## Shell: `scripts/auto-burn-in.sh`

The burner is the long-running resident that drives the entire
pre-market / market-hours / post-market cycle. The atlas covers every
shell function defined in this script.

### Boot sequence (lines 1-100)

1. **Banner print** (lines 22-29) — date stamp and feature checklist.
2. **PIN_DIR resolution** (lines 33-49) — when `PIN_DIR` is set and
   `$PIN_DIR/tradebot-local` exists, set `PINNED_TRADEBOT` and
   `PINNED_PYTHON` to the snapshot paths and `cd "$PIN_DIR"`. Otherwise
   fall back to `./tradebot-local` and `./.venv/bin/python` against the
   live worktree.
3. **Config and paths** (lines 51-89) — `CONFIG_FILE=burn-in-config.yaml`,
   `UNIVERSE_FILE=state/universe.txt`, `WATCHLIST_FILE=state/watchlist.txt`,
   `LOG_DIR=logs`, `DB_PATH=state/burn_in.db`, marker file for last
   discovery date, health state directory under `state/burn_in/`.
4. **Health artifacts** (lines 60-77) — write PID file, heartbeat file,
   EOD watchdog PID file, health log under `logs/health.jsonl`.
5. **Pre-flight checks** (lines 478-538) — `doctor`, `kill-switch`,
   `scan --symbols SPY --summary`. Any failure exits the burner.

### Shell functions

| Function | Lines | Purpose | Evidence |
| --- | --- | --- | --- |
| `write_heartbeat` | 70-77 | Writes `{ts, cycle, fills, exits, rejects}` JSON to `state/burn_in/heartbeat.json` | live heartbeat read by doctor PASS |
| `rotate_logs` | (rotates oversized logs) | log file rollover when too large | tests passing |
| `run_discovery` | discovery block | runs `discover --mode breakout --max 50 --export` via `$PINNED_TRADEBOT`; surfaces 0-candidate warning | live ran cleanly |
| `watchlist_is_low` | (heuristic) | triggers re-discovery when the universe thins | tested |
| `is_midday` | (clock check) | mid-day discovery trigger | tested |
| `should_discover` | (predicate) | first-of-day or midday or low-universe | tested |
| `sleep_until_market_open` | 360-431 | sleep in 60s chunks until next market open (handles weekends, holidays, after-hours) | tested |
| `load_symbols` | (file read) | reads `state/universe.txt` preserving final unterminated symbol | tested (`tests/test_discover_failure_visibility.py`) |
| `run_pattern_miner` | (research) | nightly pattern mining against EOD store | observed: "No patterns found" during pre-market |
| `run_nightly_tuning` | (tuning) | nightly tuning experiments | observed running cleanly |
| `run_tune_experiment_step` | (controller) | propose + evaluate experiments via `tune-experiment` | tested |
| `run_health_check` | 601-612 | invokes `doctor --burn-in --json` with `PIN_DIR` forwarded | live PID 89523 reported PASS for all 8 checks |
| `run_eod_data_download` | 614-678 | idempotent massive.com S3 fetch, gated by interval-set marker and time window | tested |
| `run_advisory_learner` | (advisory) | nightly advisory learner | opt-in |
| `on_shutdown` | (cleanup) | stops dashboard and EOD watchdog | tested |
| `ensure_dashboard` | (sidecar) | starts dashboard sidecar if not running | live :8080 listening |
| `stop_dashboard` | (sidecar) | kills dashboard sidecar | tested |
| `_manage_lock_acquire` / `_manage_lock_release` | (lock) | file lock so EOD watchdog and main loop don't race | tested |
| `start_eod_watchdog` | 819-852 | daemon-style watchdog that fires at 15:55 ET to ensure EOD exits | live PID `EOD_WATCHDOG_PID_FILE` written |
| `stop_eod_watchdog` | (cleanup) | kills EOD watchdog | tested |
| `scan_and_trade` | (cycle) | scan → paper-trade → manage-positions per cycle | tested |
| `check_confidence_gates` | (gate) | advisory-only PF alerts after 50+ trades | tested |
| `check_max_drawdown` | 1031-1102 | cohort-aware drawdown check, exits if ≥ configured threshold | tested |

### Main loop (lines 1180-1267)

```text
while true:
  timestamp + CYCLE_COUNT++
  write_heartbeat(0, 0, 0)
  run_eod_data_download()
  if should_discover():
    run_discovery(midday|daily) + run_pattern_miner + run_nightly_tuning
  load_symbols()
  if not market hours: sleep_until_market_open(); continue
  scan_and_trade()
  ensure_dashboard()
  check_max_drawdown(DB_PATH, MAX_DRAWDOWN_PCT)
  every 10 cycles: check_confidence_gates + run_advisory_learner
  every 30 cycles: run_health_check
  at 15:50 ET: run_health_check (pre-EOD safety net)
  sleep 60
```

## Shell: `scripts/burnin-launcher.sh`

The launcher captures an immutable snapshot of HEAD before exec'ing
the burner. See phase 1 for the entrypoint contract; phase 4 covers
the runtime behavior.

**Live evidence:** PID 89523, snapshot `.burnin_pin/62d178b.../`,
fingerprint `b59c537e9b0f...` written to `.burnin_pin/last_fingerprint`.

**Functions:**
- Captures `HEAD` via `capture_snapshot()` from `trading_bot/runtime/burnin_pin.py`.
- Emits `Effective runtime PIN_DIR: $SNAPSHOT_ROOT` under `PIN_DRY_RUN=1`.
- `exec "$SNAPSHOT_ROOT/scripts/auto-burn-in.sh" "$@"` — replaces the
  shell process so the burner inherits `PIN_DIR` env.

**Tests:** `tests/test_burnin_launcher_pin_export.py`,
`tests/test_burnin_runtime_pin.py`.

## Python: `trading_bot/runtime/burnin_pin.py`

**Functions:**
- `capture_snapshot(repo, pin_dir) -> PinInfo` — archives HEAD into
  `<pin_dir>/<head_sha>/` via `git archive | tar`, writes the SHA256
  fingerprint of pinned paths.
- `resolve_pin_dir(...)` — operator-facing resolver used by the
  doctor to detect a pinned snapshot at runtime.
- `PinInfo` dataclass — holds `head_sha`, `snapshot_root`,
  `fingerprint`, `python_executable`, `wrapper_path`, `burner_script`.

**Tests:** `tests/test_burnin_runtime_pin.py`.

## Python: `trading_bot/safety/kill_switch.py`

**Purpose:** Persistent kill switch via `state/kill_switch.json`.

**Live state at PID 89523:**
```
{"active": false, "reason": null, "since": null, "triggered_by": null}
```

**Tests:** `tests/test_kill_switch.py`. All pass.

## Python: `trading_bot/safety/circuit_breaker.py`

**Purpose:** Cohort-aware drawdown computation. Uses
`paper.equity_evaluation_since` (falls back to `graduation_since`)
for cohort boundaries. Returns `insufficient` when fewer than two
cohort snapshots are present, preventing false halts.

**Tests:** `tests/test_circuit_breaker.py`,
`tests/test_cohort_drawdown.py`. All pass.

## Python: `trading_bot/health/`

**Files:**
- `trading_bot/health/runner.py` — `run_health_checks(...)` runs every
  check and aggregates a `HealthReport`.
- `trading_bot/health/checks.py` — `check_pid_alive`, `check_heartbeat_fresh`,
  `check_dashboard_health`, `check_eod_watchdog`, `check_open_positions_consistent`,
  `check_market_data_freshness`, `check_tuning_experiment`, `check_scan_freshness`.
- `trading_bot/health/types.py` — `CheckResult`, `HealthReport`,
  `Status` enum (`ok`, `warn`, `critical`).

**Tests:** `tests/test_health_runner.py`, `tests/test_doctor_burn_in.py`,
`tests/test_burn_in_health_contract.py`, `tests/test_doctor_burn_in_pin_state_dir.py`.

**Live evidence at PID 89523:**
```text
[burn-in] pid_alive                    PASS  PID 89523 alive
[burn-in] heartbeat_fresh              PASS  heartbeat fresh (last 6s ago)
[burn-in] dashboard_health             PASS  dashboard :8080 health 200
[burn-in] eod_watchdog                 PASS  outside burner hours; watchdog not required
[burn-in] open_positions_consistent    PASS  no open positions
[burn-in] market_data_freshness        PASS  outside market hours
[burn-in] tuning_experiment            PASS  no active experiment
[burn-in] scan_freshness               PASS  scan fresh (last 7s ago)
```

## Python: `trading_bot/monitoring/`

**Files:**
- `trading_bot/monitoring/drawdown.py` — cohort drawdown helpers.
- `trading_bot/monitoring/health.py` — alt health aggregator (less
  complete than `trading_bot/health/runner.py`).
- `trading_bot/monitoring/notifiers.py` — notification primitives
  (currently unused; Discord wrapper was removed in commit
  `0ee60f7`).
- `trading_bot/monitoring/performance.py` — performance metrics.
- `trading_bot/monitoring/realtime_pnl.py` — real-time P&L helpers.

**Status:** mostly `verified` for the wired-in helpers; `configured
but unwired` for `notifiers.py` after the Discord wrapper removal.

## Python: `trading_bot/portfolio/performance.py`

**Purpose:** Heat computation, equity aggregation. Consumed by
`compute_portfolio_heat` calls across the runtime.

**Tests:** `tests/test_compute_portfolio_heat.py`,
`tests/test_position_management.py`. All pass.

## CLI commands exercised by the burner

- `discover` — discovery runner.
- `scan --symbols $SYMBOLS --why` — signal scan.
- `paper-trade --symbols $symbol` — paper fill.
- `manage-positions` — exit evaluation.
- `doctor` — boot-time and periodic health check.
- `kill-switch --status` — boot-time confirmation.
- `tune-experiment evaluate / propose` — nightly tuning step.
- `eod-fetch` — massive.com S3 ingestion.
- `build-universe` — universe refresh.
- `serve` (via dashboard sidecar) — long-running FastAPI.
- `backtest` — referenced in docs, not invoked by the burner.

## Cross-references

- `scripts/auto-burn-in.sh:41-49` — PIN_DIR resolution.
- `scripts/auto-burn-in.sh:601-612` — `run_health_check`.
- `scripts/auto-burn-in.sh:606-610` — `PIN_DIR` forwarded to doctor.
- `trading_bot/cli/app.py::resolve_dashboard_port` — port discovery
  via env → pin port file → settings → 8000.
- `trading_bot/cli/app.py::doctor` — `_pin_snapshot_state_dir` routes
  state_dir when a pinned snapshot is detected.
- `trading_bot/safety/circuit_breaker.py::compute_cohort_drawdown` —
  used by `check_max_drawdown` in the burner main loop.

## Open questions (will be confirmed during phase 2/3/5)

- Whether `trading_bot/monitoring/notifiers.py` should be removed
  entirely or retained for future notification integrations.
- Whether the dashboard sidecar's `ensure_dashboard` poll cadence
  matches the documented contract.
- Whether `run_advisory_learner` should respect the
  `advisory.enabled` toggle from `burn-in-config.yaml` or only the
  one-shot gating in the learner itself.
