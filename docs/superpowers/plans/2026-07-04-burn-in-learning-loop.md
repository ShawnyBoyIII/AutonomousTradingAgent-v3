# Burn-In Learning Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce supermodel over-filtering, make strategy allocation thresholds configurable, and add a nightly tuning loop that writes safe runtime overrides for burn-in.

**Architecture:** Keep the current paper-trading flow intact, but move the hardcoded supermodel and strategy-tracker thresholds into `Settings`, add an override file loaded at startup, and expose a `tune` CLI that derives small deterministic adjustments from recent paper results. Structural safety controls remain fixed and `live_trading_enabled` stays forced off after overrides load.

**Tech Stack:** Python 3.11, Typer, Pydantic v2, pytest, YAML.

## Global Constraints

- Paper-only remains enforced by `trading_bot/config/loader.py`.
- Tests stay network-free and deterministic.
- Structural risk limits such as `max_ticker_allocation_pct` and `max_portfolio_heat_pct` are not tunable by nightly overrides.
- Keep changes surgical: preserve existing CLI behavior and existing RL commands.

---

### Task 1: Configurable supermodel and tracker thresholds

**Files:**
- Modify: `trading_bot/config/settings.py`
- Modify: `trading_bot/strategy/supermodel.py`
- Modify: `trading_bot/strategy/strategy_tracker.py`
- Modify: `trading_bot/runtime/orchestrator.py`
- Test: `tests/test_supermodel.py`
- Test: `tests/test_strategy_tracker.py`

**Interfaces:**
- Produces: `SupermodelSettings`, `StrategyTrackerSettings`
- Produces: `build_stacked_signal(..., settings: SupermodelSettings | None = None)`
- Produces: `allocation_multiplier(..., settings: StrategyTrackerSettings | None = None)`

- [ ] Write failing tests for configurable thresholds and settings-aware allocation buckets.
- [ ] Run the focused tests and confirm they fail for the expected reason.
- [ ] Implement the new settings models and thread them through supermodel and orchestrator call sites.
- [ ] Re-run focused tests and confirm green.

### Task 2: Safe runtime tuning overrides

**Files:**
- Create: `trading_bot/learning/tuning_overrides.py`
- Modify: `trading_bot/config/loader.py`
- Test: `tests/test_config_loader.py`
- Test: `tests/test_tuning_overrides.py`

**Interfaces:**
- Produces: `TuningProposal`
- Produces: `load_tuning_overrides(settings: Settings, base_dir: Path) -> None`
- Produces: `write_tuning_overrides(path: Path, proposal: TuningProposal) -> None`

- [ ] Write failing tests for override loading and structural-field safety.
- [ ] Run the focused tests and confirm they fail.
- [ ] Implement proposal serialization and loader patching for allowlisted fields only.
- [ ] Re-run focused tests and confirm green.

### Task 3: Tune CLI and burn-in defaults

**Files:**
- Modify: `trading_bot/cli/app.py`
- Modify: `burn-in-config.yaml`
- Test: `tests/test_cli_smoke.py`

**Interfaces:**
- Produces: `tune` CLI command
- Produces: `state/tuning_overrides.yaml`

- [ ] Write failing CLI tests for `tradebot-local tune --dry-run` behavior.
- [ ] Run the focused tests and confirm they fail.
- [ ] Implement the command and update burn-in defaults.
- [ ] Re-run focused tests and confirm green.

### Task 4: Verification

**Files:**
- Test only.

- [ ] Run focused suites for the touched areas.
- [ ] Run a broader regression pass for loader, CLI smoke, supermodel, and tracker coverage.
- [ ] Summarize the effective behavior changes and any residual follow-up work.
