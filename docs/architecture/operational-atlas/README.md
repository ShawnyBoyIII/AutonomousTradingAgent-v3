# Operational Atlas

> Current as of 2026-07-29. Companion to `ARCHITECTURE.md` and `AGENTS.md`.
> Each subsystem map lists every operational file with purpose, callers,
> evidence, and status. Updates land in the same commit as the code change.

## Navigation

| Phase | Subsystem | Document |
| --- | --- | --- |
| 1 | Entrypoints, runtime, safety gates | [`01-entrypoints-runtime.md`](01-entrypoints-runtime.md) |
| 2 | Market data, signals, risk, execution | [`02-data-signals-execution.md`](02-data-signals-execution.md) |
| 3 | Persistence, analytics, dashboard | [`03-persistence-analytics-dashboard.md`](03-persistence-analytics-dashboard.md) |
| 4 | Burner, safety, monitoring, doctor | [`04-burner-safety-monitoring.md`](04-burner-safety-monitoring.md) |
| 5 | Learning, research, integrations | [`05-learning-research-integrations.md`](05-learning-research-integrations.md) |
| — | File-coverage index | [`file-index.md`](file-index.md) |
| — | Verification matrix | [`verification-matrix.md`](verification-matrix.md) |
| — | Remediation backlog | [`remediation-backlog.md`](remediation-backlog.md) |

## Per-file record

For every operational file the atlas records:

- **Purpose** — what the file exists to do.
- **Layer** — CLI / entry, data, strategy, risk, execution, persistence, analytics, dashboard, safety, monitoring, learning, or research.
- **Entrypoints and callers** — `file:line` references.
- **Inputs / outputs / transformations** — at the boundary.
- **Configuration and env** — what the file depends on.
- **Side effects** — DB writes, log lines, file mutations, network calls.
- **Failure behavior** — exceptions raised, fallbacks chosen.
- **Tests** — the unit, integration, and contract tests that exercise it.
- **Evidence** — recent CLI output, smoke runs, or static attestations.
- **Status** — `verified`, `partially verified`, `statically wired`, `configured but unwired`, `broken`, `network-dependent`, `manual-only`, or `read-only`.

## Status vocabulary

- **verified** — automated tests and at least one focused smoke exercise the boundary; no exceptions or unmet assumptions were found.
- **partially verified** — tests cover the happy path; recovery paths or rare branches remain untested.
- **statically wired** — code paths exist and imports resolve, but no dynamic evidence (test or smoke) has exercised them.
- **configured but unwired** — config flags exist with no production callers.
- **broken** — known defect with file:line evidence; remediation tracked.
- **network-dependent** — operates correctly only with live network access; tests use monkeypatched fakes.
- **manual-only** — only safe under explicit operator invocation; the automated paths avoid it.
- **read-only** — verified via static inspection only; no execution evidence required for documentation purposes.

## Safety rules used while writing this atlas

- No `scan`, `paper-trade`, `manage-positions`, or `continuous` invocations.
- No synthetic fills against the live ledger.
- No kill-switch mutations, no runtime restarts, no tuning/advisory/research mutations.
- All smoke checks use `--config-path burn-in-config.yaml` and target the pinned snapshot (`state/burn_in/` under the active `.burnin_pin/<HEAD>/`).
- DB inspection uses SQLite `mode=ro` URIs and never writes.

## Build sequence

1. Inventory: `git ls-files` over operational roots; 185 files indexed.
2. Phase maps: traced via static analysis + targeted test/smoke runs.
3. Verification matrix: each subsystem row mapped to passing tests or explicit static attestations.
4. Remediation backlog: confirmed defects separated from architectural risks.
5. Browser diagrams: render cross-system views once phase maps are stable.
</content>
</invoke>