# Next-Open Backtest Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove same-close entry optimism from intraday replay by filling approved signals at the next observed bar open.

**Architecture:** Keep the existing signal, supermodel, counter-thesis, sizing, and `PaperBroker` paths. Store an approved setup as a pending entry, reprice it from the next bar's open, reject invalid gaps, and then submit it through the existing risk and execution seam.

**Tech Stack:** Python 3.11+, pandas, Pydantic, pytest.

## Global Constraints

- Do not add dependencies or copy FinceptTerminal source.
- Preserve configured paper fees and slippage.
- A next open at or outside the original stop/target invalidates the pending setup.
- The existing risk manager must re-evaluate the repriced setup.

---

### Task 1: Define next-open repricing behavior

**Files:**
- Create: `tests/test_backtest_next_open_execution.py`
- Modify: `trading_bot/backtest/runner.py`

**Interfaces:**
- Consumes: `TradeSignal`, a pandas bar containing `open` and `timestamp`.
- Produces: `_signal_at_next_open(signal, bar) -> TradeSignal | None`.

- [x] **Step 1: Write failing tests** proving the entry price and timestamp move to the next bar, risk/reward is recalculated, and stop/target gaps return `None`.
- [x] **Step 2: Run** `.venv/bin/python -m pytest tests/test_backtest_next_open_execution.py -q` and confirm failure because `_signal_at_next_open` does not exist.
- [x] **Step 3: Implement** the minimal helper with `TradeSignal.model_copy(update=...)` and no new abstraction.
- [x] **Step 4: Re-run** the focused test and confirm it passes.

### Task 2: Queue entries in causal intraday replay

**Files:**
- Modify: `tests/test_backtest_next_open_execution.py`
- Modify: `trading_bot/backtest/runner.py`

**Interfaces:**
- Consumes: the approved signal and sizing metadata already produced per bar.
- Produces: one pending entry that is submitted at the next bar open through `submit_signal_as_order`.

- [x] **Step 1: Add a failing integration test** that distinguishes next-open P&L from same-close P&L.
- [x] **Step 2: Run the focused test** and confirm the current immediate-fill implementation fails it.
- [x] **Step 3: Replace immediate submission** with a single pending-entry record and execute it before exit evaluation on the following bar.
- [x] **Step 4: Run causal, parity, and execution tests** and correct only regressions caused by the timing change.

### Task 3: Document and verify the replay contract

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Produces: an accurate operational statement of signal and fill timing.

- [x] **Step 1: Update the intraday backtest contract** to state next-bar-open fills and invalid-gap cancellation.
- [x] **Step 2: Run the complete test suite** with `.venv/bin/python -m pytest -q` (`2024 passed`).
- [x] **Step 3: Inspect `git diff`** and confirm no unrelated work was reverted or rewritten.
