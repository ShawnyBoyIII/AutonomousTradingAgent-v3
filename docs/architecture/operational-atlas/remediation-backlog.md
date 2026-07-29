# Remediation Backlog

> Generated 2026-07-29 at HEAD `62d178b`. Severity rankings follow the
> audit's stop-the-line threshold: a defect that affects a live
> operational surface (running burner, dashboard, ledger, kill switch)
> is **Critical**; everything else is **Important** (latent risk,
> defensive gap) or **Cosmetic** (clarity, dead code).

## Severity legend

- **Critical** — actively affects a running operational surface. Was
  promoted above the audit's normal "map first" rule because the
  burner would be poisoned, the dashboard would read the wrong DB, or
  a CLI command would crash before the loop started.
- **Important** — latent defect or contract gap that future refactors
  could re-open. Should be planned.
- **Cosmetic** — dead code, stale comment, or ergonomic improvement.
  Bundled with related changes.

## Fixed in this audit (do not reopen)

| Commit | Defect | Severity | Tests added |
| --- | --- | --- | --- |
| `bbc986f` | `continuous` CLI passed removed `use_event_system` to `run_continuous_loop`. | Critical | `tests/test_continuous_cli.py` |
| `82d1700` | `load_settings()` ignored `CONFIG_PATH` env; CLI `serve` did not propagate to Uvicorn. | Critical | `tests/test_config_path_routing.py` |
| `26c6ec7` | Launcher exported `PIN_DIR` to the snapshot parent, not the snapshot root; auto-burn-in resolved `$PIN_DIR/tradebot-local` from the wrong directory and silently fell back to the live wrapper. | Critical | `tests/test_burnin_launcher_pin_export.py` |
| `85631c7` | Manual `./tradebot-local doctor --burn-in` from outside the burner read stale `state/burn_in/...` because the burner wrote to `$PIN_DIR/state/burn_in/...`. | Important | `tests/test_doctor_burn_in_pin_state_dir.py` |
| `62d178b` | `run_health_check` invoked the doctor subprocess without forwarding `PIN_DIR`; the subprocess lost the snapshot's health-state contract. | Important | `tests/test_auto_burn_in_script.py::test_run_health_check_forwards_pin_dir_to_doctor` |

All five commits are merged into `main` and pushed to `v3/main`. The
burner is currently pinned at HEAD `62d178b` with PID 89523.

## Important (planned)

None at HEAD 62d178b. Phase 2 through 5 of the atlas may surface new
items; they will be appended here in the same commit that introduces
the corresponding test.

## Hypotheses still pending verification (will be confirmed during phase 2/3/5)

The following hypotheses were raised during the entrypoint audit and
were not investigated to avoid drifting from the repair cycle. They
will be re-examined during the corresponding phase map and converted
into either confirmed defects (added here) or documented invariants
(added to the verification matrix).

1. **Fill persistence atomicity (Phase 3).** A trade is recorded in two
   steps — `record_fill()` writes to `orders`, then `update_trade_exit()`
   updates P&L fields. If the process crashes between the two writes,
   the `orders` row exists without a paired `trades` row. Cross-table
   reporting reads both tables; reconciliation should be observable.
2. **`validate_bars()` keyword mismatch (Phase 2).** Earlier hypothesis
   that the function may be called with unsupported keywords from a
   code path that hasn't yet been wired to the V2.5 fail-fast contract.
3. **Counter-thesis provider bypass (Phase 2).** The configured-provider
   bypass hypothesis was raised because `fetch_counter_thesis_context`
   is the only network-touching entry but several integrations appear
   manual-only.
4. **V3 sizing multiplier (Phase 2).** A unused V3 sizing multiplier was
   referenced in an earlier audit and never confirmed resolved.
5. **Configured research integrations (Phase 5).** Several configurations
   appear manual-only or unwired. These need explicit status tags once
   the phase 5 audit completes.

## Cosmetic (deferred)

- `auto-burn-in.sh:31` hardcodes the live worktree path; not exercised
  in the pinned path because `cd "$PIN_DIR"` overrides it. Will be
  removed when the manual fallback branch is no longer required.
- Three redundant `.venv/bin/python -c` JSON parses in
  `burnin-launcher.sh:77-79`. Cosmetic; cosmetic performance, not a
  correctness issue.

## Reviewer-flagged notes

- The pre-existing pandas mixed-timezone `FutureWarning` at
  `trading_bot/data/cache.py:274` is unrelated to the audit; will be
  addressed in a future data-cleanup pass.
- Pydantic serializer warning in
  `tests/test_runtime_canary_controller.py::test_evaluate_marks_unsupported_change_inconclusive`
  is a test-only warning, not a production defect.
