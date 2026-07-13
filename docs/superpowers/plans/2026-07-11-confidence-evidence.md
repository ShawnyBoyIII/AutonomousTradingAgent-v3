# Confidence Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate post-cap paper results into a configured graduation cohort and restore focused coverage for active decision gates without changing fire-mode thresholds.

**Architecture:** Reuse the existing `summarize_paper_performance(..., since, until)` path. Add one optional timestamp to paper settings, route graduation through the same window parser used by paper reporting, and cover the still-active supermodel/counter-thesis logic with small network-free tests.

**Tech Stack:** Python 3.11+, Pydantic v2, Typer, SQLite, pytest, YAML.

## Global Constraints

- Preserve every existing fire-mode risk and strategy threshold.
- Keep paper-only enforcement and Robinhood MCP-only behavior unchanged.
- Do not mutate or delete historical paper records.
- Add no dependencies and perform no network calls in tests.

---

### Task 1: Configured Graduation Cohort

**Files:**
- Modify: `trading_bot/config/settings.py`
- Modify: `burn-in-config.yaml`
- Modify: `trading_bot/cli/app.py`
- Test: `tests/test_paper_performance.py`

**Interfaces:**
- Consumes: `summarize_paper_performance(db_path, since, until)`
- Produces: `PaperSettings.graduation_since: datetime | None` and `graduation-check --since/--until`

- [ ] **Step 1: Write failing tests** proving the configured timestamp filters legacy rows and an explicit `--since` overrides it.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_paper_performance.py -q` and confirm the new tests fail because the setting/options do not exist.
- [ ] **Step 3: Add** `graduation_since: datetime | None = None` to `PaperSettings`, set `2026-07-11T00:00:00+00:00` in burn-in config, and pass resolved `since`/`until` values into `summarize_paper_performance()` from `graduation_check`.
- [ ] **Step 4: Run** `.venv/bin/python -m pytest tests/test_paper_performance.py -q` and confirm it passes.

### Task 2: Active Decision-Gate Coverage

**Files:**
- Create: `tests/test_active_decision_gates.py`
- Read only: `trading_bot/strategy/supermodel.py`
- Read only: `trading_bot/strategy/counter_thesis.py`

**Interfaces:**
- Consumes: `build_stacked_signal`, `evaluate_counter_thesis`, `CounterThesisContext`
- Produces: regression coverage only; no production behavior change

- [ ] **Step 1: Add focused tests** for no-signal neutrality, counter-thesis-backed supermodel blocking, clean counter-thesis approval, and severe regime blocking.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_active_decision_gates.py -q` and confirm all tests pass against current intended behavior.
- [ ] **Step 3: If behavior differs**, stop and diagnose before changing production code; do not rewrite assertions to bless an unexplained result.

### Task 3: Operator Documentation and Verification

**Files:**
- Modify: `AGENTS.md`
- Test: `tests/test_dangling_cli_refs.py`

**Interfaces:**
- Consumes: current Typer command registry
- Produces: operator guidance containing only runnable commands

- [ ] **Step 1: Add a failing assertion** that all backticked `./tradebot-local` commands documented in AGENTS are registered or are real scripts.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_dangling_cli_refs.py -q` and confirm it fails on `supermodel-report`.
- [ ] **Step 3: Remove stale `supermodel-report` references** and document `graduation-check` as cohort-aware.
- [ ] **Step 4: Run focused tests**, then `.venv/bin/python -m pytest -q`.
- [ ] **Step 5: Run** `./tradebot-local --config-path burn-in-config.yaml graduation-check` and confirm the report starts at the configured cohort timestamp and does not graduate with fewer than 100 cohort trades.

