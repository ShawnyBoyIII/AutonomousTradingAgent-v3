# Confidence Evidence Design

## Goal

Make paper graduation measure only trades produced after the current 50-share
cap, while preserving the approved fire-mode thresholds.

## Design

`PaperSettings` owns an optional `graduation_since` UTC timestamp. The burn-in
configuration sets it to `2026-07-11T00:00:00+00:00`, creating a clean evidence
cohort without deleting historical trades. `graduation-check` accepts optional
`--since` and `--until` overrides and otherwise uses the configured start.

The existing `summarize_paper_performance()` windowing remains the only metrics
path. No parallel cohort database or new reporting abstraction is introduced.
The command prints the selected window, retains the existing 100-trade and
profit-factor gates, and therefore cannot graduate on legacy fire-mode trades.

Active supermodel and counter-thesis production paths receive focused,
network-free regression tests to replace the useful coverage lost during the
RL teardown. Documentation stops advertising the removed `supermodel-report`
command and points operators to the surviving evidence commands.

## Constraints

- Preserve all current fire-mode thresholds in `burn-in-config.yaml`.
- Preserve paper-only enforcement and Robinhood MCP-only integration.
- Do not delete or mutate historical paper records.
- Add no dependencies and make no network calls in tests.
- Keep explicit CLI timestamps higher priority than configured timestamps.

## Verification

- Focused tests prove configured and explicit cohort selection.
- Focused tests prove active supermodel blocking and counter-thesis behavior.
- CLI smoke verifies the removed command is no longer documented.
- Full pytest suite remains green.

