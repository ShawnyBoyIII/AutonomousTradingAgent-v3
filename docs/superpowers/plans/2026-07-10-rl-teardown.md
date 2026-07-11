# RL Teardown Completion Plan

> Read-only plan; executed subagent-by-subagent.

**Goal**: Finish the in-progress teardown of the RL (reinforcement learning) subsystem so the codebase stops referencing a deleted package, and so docs/operator UI no longer advertise RL commands that no longer exist.

**Scope**:
- Surgical deletes only. Preserve V2.5 + V3 signal paths.
- Supermodel scope: keep if non-RL, drop the `rl_buy/rl_hold/...` summary fields and references to RL action codes if they're only there to feed RL display.

**Tech Stack**: Python 3.11, Typer, Pydantic v2, pytest, YAML.

## Global Constraints

- Paper-only remains enforced by `trading_bot/config/loader.py`.
- Tests stay network-free and deterministic.
- Existing user-facing CLI commands other than RL-related must still work.
- Keep AGENTS.md accurate (this *is* an AGENTS.md-touching change).
- All changes must remain surgical; preserve EOD data-store stack and burn-in reliability work.

## File Structure (proposed)

| File | Change |
|---|---|
| `scripts/train_rl.py` | **Delete** |
| `scripts/train_rl_gpu.py` | **Delete** |
| `scripts/sector_diversity_rl.py` | **Delete** |
| `scripts/auto_retrain_trigger.py` | **Delete** |
| `tests/test_rl_cli.py`, `tests/test_rl_actions.py`, `tests/test_rl_backtest.py`, `tests/test_rl_env.py`, `tests/test_rl_ensemble.py`, `tests/test_rl_features.py`, `tests/test_rl_labels.py`, `tests/test_rl_rewards.py`, `tests/test_rl_rewards_extended.py`, `tests/test_rl_utils.py`, `tests/test_train_rl_gpu.py`, `tests/test_sector_diversity_rl.py` | **Already deleted** in working tree |
| `tests/test_auto_retrain_trigger.py`, `tests/test_live_data_collector.py` (if exist, related to RL) | Investigate; remove if RL-only |
| `tests/test_backtest_runner.py` | Already partially pruned — verify no remaining RL imports remain |
| `tests/test_cli_smoke.py` | Already partially pruned — verify no remaining RL imports |
| `trading_bot/cli/app.py` | Remove `run_rl_backtest` reference, `rl_model_meta_path`/`rl_model_symbols` references, rl-* commands (rl-train, rl-eval, rl-benchmark, rl-model-info, rl-scan-plan, rl-walkforward, rl-auto-retrain). Drop `summary["rl_buy"/...]` display and `_scan_row_supermodel`/RL-specific supermodel `_scan_row_details` usage |
| `trading_bot/backtest/runner.py` | Remove `run_rl_backtest`, `run_rl_walk_forward` |
| `trading_bot/backtest/attribution.py` | Remove "run_rl_backtest" mention |
| `scripts/daily_supermodel.py` | Remove RL imports + `RLAgent(...)` invocation in `train_supermodel()` |
| `pyproject.toml` | Remove `rl = [...]` extras block, remove `rl.*` optional CLI group if present |
| `pyproject.toml` scripts | Drop `train-rl-*`, `rl-compare`-style entry points that reference deleted modules |
| `config.yaml`, `burn-in-config.yaml` | Drop the `rl:` block; replace `settings.rl` typed model with a small compat note or remove |
| `AGENTS.md` | Update "RL (research lane, disabled in burn-in)" section → remove RL commands; update "Common Commands" and "Session Gotchas" |
| `README.md` | Remove "RL Research Lane" claim and command examples |
| `QUICK_REFERENCE.md` | Drop `rl-*` rows |
| `GETTING_STARTED.md` | Drop `rl-*` examples |
| `docs/RL_TRADING_GUIDE.md` | **Delete** (or rewrite as `REMOVED.md`) |
| `docs/SUPERMODEL_V1.md` | Drop RL layer references |
| `scripts/README_GPU_TRAINING.md` | **Delete** if RL-only |
| `tests/test_supermodel.py`, `tests/test_supermodel_replay.py`, `tests/test_supermodel_stack.py` | Already deleted in working tree — verify |
| `tests/test_advisory_learner.py`, `tests/test_counter_thesis.py` | Already deleted — verify no functional impact (counter-thesis is still in source) |
| `trading_bot/rl/` | Already deleted — verify |

## Tasks

### Task 1: Inventory all remaining RL references

For each file identified above:
1. Open in `cat`/`grep`.
2. Confirm whether the file still references `trading_bot.rl` or any `rl_*` symbol.
3. If yes, plan removal (delete function/import, leave a stub call if part of orchestrator CLI dispatch chain).
4. If no, file is already done.

### Task 2: Remove RL scripts and RL ref in `daily_supermodel.py`

- Delete `scripts/train_rl.py`, `scripts/train_rl_gpu.py`, `scripts/sector_diversity_rl.py`, `scripts/auto_retrain_trigger.py`.
- In `scripts/daily_supermodel.py`: drop `from trading_bot.rl.*` and the `RLAgent(...)` invocation in `train_supermodel()`. Keep the EOD data-store coverage logging I added (separate feature stack).

### Task 3: Strip RL from CLI surface

`trading_bot/cli/app.py`:
- Drop `_validate_rl_model_symbols`, references to `rl_model_meta_path`, `rl_model_symbols`.
- Drop `from trading_bot.backtest.runner import run_rl_backtest` import inside the relevant command (likely `backtest --rl`).
- Drop summary fields `rl_*` in scan/paper-trade output blocks.
- Remove `rl-compare`, `rl-train`, `rl-eval`, `rl-benchmark`, `rl-model-info`, `rl-scan-plan`, `rl-walkforward`, `rl-auto-retrain` typer commands.

### Task 4: Strip RL from backtest

`trading_bot/backtest/runner.py`:
- Remove `run_rl_backtest`, `run_rl_walk_forward`. Remove `from trading_bot.rl.*` import.

`trading_bot/backtest/attribution.py`:
- Clean comment that says `run_rl_backtest`.

### Task 5: Strip RL from `pyproject.toml`

- Remove `rl = [...]` extra block.
- Remove any `train-rl`, `rl-compare`, `auto-retrain` script entry points.
- Verify `dependencies` no longer mentions `gymnasium`, `stable-baselines3`, `torch`. Keep `torch` only if used elsewhere; check.

### Task 6: Strip RL from config files

- `config.yaml`, `burn-in-config.yaml`: remove `rl:` blocks.
- Update `settings.py` — `settings.rl` is already `dict`; consider whether to remove the field entirely. Test impact first.

### Task 7: Update docs

- `AGENTS.md`: drop "RL (research lane, disabled in burn-in)" section, "Common Commands" lines, and any `rl-*` mentions in Session Gotchas.
- `README.md`: drop "RL Research Lane" claim and command examples.
- `QUICK_REFERENCE.md`: drop `rl-*` rows.
- `GETTING_STARTED.md`: drop `rl-*` examples; reference EOD data store if needed.
- `docs/RL_TRADING_GUIDE.md`: delete.
- `docs/SUPERMODEL_V1.md`: drop RL layer references.
- `scripts/README_GPU_TRAINING.md`: delete.

### Task 8: Tests

Run after each task:
- `tests/test_cli_smoke.py -q` — must end green
- `tests/test_backtest_runner.py -q` — must end green
- `tests/test_live_safety.py -q` — must end green

Then full suite:
- `.venv/bin/python -m pytest -q` — target 0 unexpected failures (excluding unrelated pre-existing EOD stack failure if any).

### Task 9: Commit & summary

Single commit: `chore: complete RL teardown (CLI, scripts, docs, pyproject)`.

## Self-Review (post-write)

**Placeholder scan:** No "TBD"/"TODO" left for RL future work.
**Type consistency:** `settings.rl` field removal must not break any other config-load path.
**Edge cases:** Operator-facing commands that mention `rl-*` must no longer appear in `--help`.
