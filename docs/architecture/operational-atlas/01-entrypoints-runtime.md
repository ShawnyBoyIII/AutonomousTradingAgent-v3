# Phase 1 — Entrypoints and Runtime

> 2026-07-29 snapshot at HEAD `62d178b`. Verified live at PIN_DIR
> `.burnin_pin/62d178b.../` with PID `89523`. Manual
> `PIN_DIR=... ./tradebot-local --config-path burn-in-config.yaml doctor --burn-in`
> returned `worst=PASS` for all eight checks.

## Shell wrapper

### `tradebot-local`
**Purpose:** Per-launch python wrapper that resolves a worktree-specific
`PYTHONPATH` so `trading_bot` imports from the burner's snapshot when
`PIN_DIR` is set, or from the live tree otherwise.

**Callers:** invoked by `scripts/auto-burn-in.sh` (`$PINNED_TRADEBOT`),
`scripts/start-dashboard.sh`, and operators directly.

**Tests:** `tests/test_burnin_runtime_pin.py::test_wrapper_uses_pin_dir_when_set`,
`test_wrapper_falls_back_without_pin_dir`. Both pass under HEAD.

**Status:** verified.

## Typer application

### `trading_bot/main.py`
**Purpose:** Console-script `tradebot` and `python -m trading_bot`
entry. Loads `.env`, delegates to `trading_bot.cli.app.app`.

**Tests:** covered by Typer command smoke tests (`tests/test_cli_smoke.py`).

**Status:** verified.

### `trading_bot/cli/app.py`
**Purpose:** Typer root callback and command dispatch (160+ commands).

**Subcommands:** `serve`, `continuous`, `paper-trade`, `manage-positions`,
`scan`, `portfolio`, `kill-switch`, `tune-experiment`, `advisory-learn`,
`advisory-report`, `backtest`, `discover`, `eod-fetch`, `doctor`.

**Configuration:** CLI flag > `CONFIG_PATH` env > `config.yaml`
(`load_settings`). The `serve` command re-exports the resolved absolute
path before `uvicorn.run("ui.dashboard.main:app", ...)` so the
dashboard's module-level `DashboardState()` reads the same config.

**Safety gates:** root callback runs `_setup_kill_switch`; each trading
command invokes the kill switch and circuit breakers before any side effect.

**Tests:** `tests/test_cli_smoke.py`, `tests/test_config_path_env.py`,
`tests/test_config_path_routing.py`, `tests/test_continuous_cli.py`,
`tests/test_runtime_canary_cli.py`. All pass.

**Status:** verified.

### `trading_bot/cli/__init__.py`
**Purpose:** Marks `trading_bot.cli` as a package.

**Status:** static.

## Entrypoint scripts

### `scripts/burnin-launcher.sh`
**Purpose:** Captures the immutable snapshot under `PIN_PARENT_DIR/<HEAD>/`,
emits a SHA256 fingerprint, then exports `PIN_DIR=<pin_parent>/<HEAD>/`
and `exec`s `scripts/auto-burn-in.sh` from the snapshot root.

**Inputs:** `BURNIN_CONFIG` (default `burn-in-config.yaml`),
`PIN_PARENT_DIR` (default `$REPO/.burnin_pin`), `PIN_DRY_RUN`.

**Side effects:** writes `.burnin_pin/<HEAD>/` snapshot,
`.burnin_pin/last_fingerprint`. Prints `Effective runtime PIN_DIR:`
under `PIN_DRY_RUN=1`.

**Tests:** `tests/test_burnin_launcher_pin_export.py` (5 additive tests).

**Status:** verified.

### `scripts/auto-burn-in.sh`
**Purpose:** Long-running resident burner. Maintains
`$HEALTH_STATE_DIR` (PID, heartbeat, dashboard port, EOD watchdog PID),
runs discovery, EOD ingestion, scan, paper-trade, manage-positions,
nightly tuning, and health-check loops.

**Inputs:** `PIN_DIR` (resolved by launcher; consumed at lines 41-49),
`BURNIN_CONFIG`, `DASHBOARD_PORT`, `AUTO_DASHBOARD`, `EOD_DATA_STORE`.

**Side effects:** writes `state/burn_in.db`, `state/burn_in/`, `logs/`,
fetches via `eod-fetch`. Forwards `PIN_DIR` to the doctor subprocess
at line 610.

**Tests:** `tests/test_auto_burn_in_script.py` (12 tests),
`tests/test_auto_burn_in_market_hours.py`,
`tests/test_burn_in_health_contract.py`,
`tests/test_burnin_launcher_pin_export.py`,
`tests/test_burnin_runtime_pin.py`. All pass.

**Status:** verified.

### `scripts/start-dashboard.sh`
**Purpose:** Standalone dashboard launcher. Sets `CONFIG_PATH=$CONFIG`
and invokes `python -m uvicorn ui.dashboard.main:app`.

**Tests:** `tests/test_auto_burn_in_script.py::test_start_dashboard_*`,
`tests/test_dashboard_config_routing.py`.

**Status:** verified.

### `scripts/burn-in-monitor.sh`, `scripts/burn-in-weekly-review.sh`, `scripts/daily-start.sh`
**Purpose:** Operator convenience scripts that wrap `tradebot-local`
doctor, paper-report, and trade-attribution. Network-free and
idempotent.

**Status:** verified (operator-only).

### `scripts/auto_bench_cron.py`
**Purpose:** Optional alpha-factor benchmark scheduler.

**Tests:** `tests/test_alpha_factors.py`.

**Status:** verified.

### `scripts/security-harden.sh`
**Purpose:** Sets restrictive file permissions on the operator machine.

**Status:** verified (operator-only).

## Bootstrap pipeline

### `trading_bot/logging_config.py`
**Purpose:** `setup_logging()` and `configure_from_settings(settings)`.
Idempotent handler installation via `_HANDLER_MARKER`.

**Status:** verified.

### `trading_bot/config/loader.py`
**Purpose:** YAML loader that maps a config path into the `Settings`
Pydantic model. Honors `explicit path > CONFIG_PATH env > config.yaml`.
Validates no credentials, applies env overrides, applies allowlisted
tuning overrides, forces `live_trading_enabled=false`.

**Tests:** `tests/test_config_path_env.py`,
`tests/test_config_path_routing.py`, `tests/test_config_loader.py`,
`tests/test_burn_in_tuning_2026_07_10.py`.

**Status:** verified.

### `trading_bot/config/settings.py`
**Purpose:** Pydantic settings model covering `app`, `risk`, `strategy`,
`counter_thesis`, `supermodel`, `strategy_tracker`, `market_data`,
`paper`, `execution`, `advisory`, `health`, `eod_data_store`,
`tuning_overrides`. Sub-models enforce per-field constraints.

**Status:** verified.

## Runtime helpers

### `trading_bot/runtime/burnin_pin.py`
**Purpose:** `capture_snapshot(repo, pin_dir)` archives `HEAD` via
`git archive` and records SHA256 fingerprint of pinned paths.
Exposes `PinInfo`, `resolve_pin_dir`, `resolve_tradebot_local`.

**Tests:** `tests/test_burnin_runtime_pin.py::test_pin_helper_*`,
`test_pin_snapshot_is_immutable_to_live_mutation`.

**Status:** verified.

### `trading_bot/runtime/continuous_loop.py`
**Purpose:** `run_continuous_loop()` — long-running paper-trading loop.
Removed event-system execution in commit `4bff394`.

**Tests:** `tests/test_continuous_loop.py` family (28 tests passing).

**Status:** verified.

### `trading_bot/runtime/orchestrator.py`
**Purpose:** Single-cycle orchestrator: discovery → scan → paper-trade →
manage-positions. Used by both CLI commands and the continuous loop.

**Tests:** `tests/test_paper_trade.py`, `tests/test_runtime_canary_cli.py`,
`tests/test_position_management.py`.

**Status:** verified.

### `trading_bot/runtime/session.py`
**Purpose:** Session-level helpers (market hours, calendar boundaries).

**Status:** verified.

## Safety gates

### `trading_bot/safety/kill_switch.py`
**Purpose:** Persistent kill switch via `state/kill_switch.json`. Reads/
writes a JSON envelope with `active`, `reason`, `since`, `triggered_by`.

**Tests:** `tests/test_kill_switch.py`.

**Status:** verified.

### `trading_bot/safety/circuit_breaker.py`
**Purpose:** Cohort-aware drawdown computation and circuit-breaker
evaluation. Uses `paper.equity_evaluation_since` (falls back to
`graduation_since`) for cohort boundaries. Returns `insufficient` when
fewer than two cohort snapshots are present.

**Tests:** `tests/test_circuit_breaker.py`,
`tests/test_cohort_drawdown.py`.

**Status:** verified.

## Cross-references

- `scripts/burnin-launcher.sh:96-100` exports `PIN_DIR` to the snapshot root.
- `scripts/auto-burn-in.sh:41-49` consumes `PIN_DIR` and pins child paths.
- `scripts/auto-burn-in.sh:606-610` forwards `PIN_DIR` to the doctor subprocess.
- `trading_bot/cli/app.py::resolve_dashboard_port` consults both
  `$PIN_DIR/state/burn_in/dashboard.port` and the live tree.
- `trading_bot/cli/app.py::doctor` resolves state_dir and
  eod_watchdog_pid_file through `_pin_snapshot_state_dir` when a pinned
  snapshot is detected.
