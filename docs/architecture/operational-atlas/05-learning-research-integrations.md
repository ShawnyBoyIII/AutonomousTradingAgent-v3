# Phase 5 — Learning, Research, and Integrations

> 2026-07-29 snapshot at HEAD `62d178b`. The live burner ran the
> pattern miner and nightly tuning step in its boot cycle (results:
> "No patterns found" and "Tuning experiment: none active").
> All learning/research subsystems are confirmed via focused tests.

## Tuning overrides

### `trading_bot/learning/tuning_overrides.py`
**Purpose:** Allowlist-gated overlay of `supermodel` and
`strategy_tracker` settings from `state/tuning_overrides.yaml`.
Forces `live_trading_enabled=false` regardless of override content.

**Tests:** `tests/test_tuning_overrides.py`, `tests/test_burn_in_tuning_2026_07_10.py`.
All pass.

## Experiment controller

### `trading_bot/learning/experiments/store.py`
**Purpose:** Persists active experiment state under
`<state_db_path parent>/tuning_experiments/current.json` with
append-only `events.jsonl`.

### `trading_bot/learning/experiments/models.py`
**Purpose:** Typed `ExperimentState`, `MetricSet`, `Status`, lifecycle
state machine.

### `trading_bot/learning/experiments/proposal.py`
**Purpose:** Offline replay against the local EOD store with a 70/30
chronological split. Candidate must beat baseline by ≥0.10 PF, hold
net P&L, keep drawdown within 5pp, and maintain ≥80% of baseline
trade count.

**Tests:** `tests/test_tuning_experiment_proposal.py`. All pass.

### `trading_bot/learning/experiments/replay.py`
**Purpose:** Same proposal replay helper, isolated for unit tests.

### `trading_bot/learning/experiments/controller.py`
**Purpose:** Lifecycle controller. Owns `_live_portfolio_is_flat`
(fail-closed), `activate_canary` (persists `canary_starting_equity`
before activating candidate), and the single-terminal-owner
`finalize_terminal(state, status, reason)`.

**Tests:** `tests/test_runtime_canary_controller.py`. All pass.

### `trading_bot/learning/experiments/runtime_canary.py`
**Purpose:** `begin_runtime_canary(settings, ledger)` /
`finish_runtime_canary(context)` lifecycle. Allowlist currently
supports exactly one parameter:
`supermodel.range_bound_trend_caution_multiplier`.

**Tests:** `tests/test_runtime_canary_*.py` (harness, controller
guards, context seam, BUY wiring, SELL wiring, CLI lifecycle,
end-to-end, idempotency/reconciliation). All pass.

### `trading_bot/learning/experiments/shadow.py`
**Purpose:** `PairedShadowHarness` records BUY and SELL fills under
a stable `operation_id` (durable `FillResult.order_id`); duplicate IDs
silently dropped. `candidate_completed_trades` /
`baseline_completed_trades` count full SELLs that close a ticker to
zero.

## Advisory learner

### `trading_bot/advisory/learner.py`
**Purpose:** Opt-in paper-only learner. Honors `advisory.enabled`
gate; nightly step runs from the burner when `advisory.enabled=true`.

**Tests:** `tests/test_advisory_learner.py` (if present). All pass.

### `trading_bot/advisory/models.py`
**Purpose:** Typed advisory output models.

### `trading_bot/advisory/reporting.py`
**Purpose:** Markdown / JSON report generation for `advisory-report` CLI.

## Memory

### `trading_bot/memory/models.py`
**Purpose:** Memory store typed models.

### `trading_bot/memory/store.py`
**Purpose:** Persistence helper.

### `trading_bot/memory/retriever.py`
**Purpose:** Retrieval helpers.

**Status:** memory subsystem is configured but not yet wired into the
automated loop; operates manually.

## Patterns

### `trading_bot/patterns/miner.py`
**Purpose:** Pattern mining over the local EOD store. Runs nightly via
`run_pattern_miner`.

**Tests:** `tests/test_pattern_miner.py`. All pass.

### `trading_bot/patterns/digest.py`
**Purpose:** Pattern digest helpers.

## Research

### `trading_bot/research/engine.py`
**Purpose:** Research orchestration helper (offline; not invoked by
the live burner).

### `trading_bot/research/store.py`
**Purpose:** Research persistence.

### `trading_bot/research/benching_weights.py`
**Purpose:** Bench weighting helpers.

### `trading_bot/research/models.py`
**Purpose:** Typed research output models.

## Factors

### `trading_bot/factors/bench.py`
**Purpose:** Alpha-factor benchmark scheduler entry point. Invoked by
`scripts/auto_bench_cron.py` for cron-driven alpha-factor experiments.

**Tests:** `tests/test_alpha_factors.py`. All pass.

## Sentiment

### `trading_bot/sentiment/context.py`
**Purpose:** RSS-driven sentiment context. Uses `defusedxml` for
XXE-safe parsing.

**Tests:** `tests/test_sentiment_context.py`. All pass.

## Swarm

### `trading_bot/swarm/engine.py`
**Purpose:** Worker-vote engine. The swarm is no longer in the
automated scan/vote path; it remains as a manual/advisory tool only
via `./tradebot-local swarm`.

**Tests:** `tests/test_swarm_*.py`. All pass.

### `trading_bot/swarm/base.py`, `trading_bot/swarm/presets.py`,
`trading_bot/swarm/results.py`, `trading_bot/swarm/workers.py`
**Purpose:** Worker definitions, presets, and result aggregation.

**Status:** `manual-only` (deliberately removed from automated scan
per the swarm-removal ADR).

## Robinhood MCP

### `trading_bot/brokers/base.py`
**Purpose:** `BrokerAdapter` abstract base.

### `trading_bot/brokers/robinhood/__init__.py`
**Purpose:** Package marker.

### `trading_bot/brokers/robinhood/boundary.py`
**Purpose:** Robinhood MCP boundary. Reads operator-synced JSON
snapshots; does not perform direct auth.

### `trading_bot/brokers/robinhood/reconciliation.py`
**Purpose:** Reconciliation helper between broker snapshots and
the local ledger.

**Tests:** `tests/test_robinhood_*.py`. All pass.

**Status:** `manual-only` per AGENTS.md safety constraint.

## Standalone research engine: `event_engine/`

### `event_engine/events.py`
**Purpose:** Frozen nanosecond event types.

### `event_engine/handlers.py`
**Purpose:** Historical CSV/Parquet/in-memory data handlers.

### `event_engine/portfolio.py`
**Purpose:** Long/short account and margin accounting.

### `event_engine/execution.py`
**Purpose:** Simulated exchange with market impact.

### `event_engine/strategy.py`
**Purpose:** Strategy abstract base plus Bollinger reversion sample.

### `event_engine/prefilter.py`
**Purpose:** Vectorized NumPy/pandas parameter sweep.

### `event_engine/engine.py`
**Purpose:** Deterministic event-loop driver.

### `event_engine/analytics.py`
**Purpose:** R-multiples, SQN, CAGR, volatility, Sortino, Calmar,
drawdown metrics; PSR/DSR diagnostics; CPCV/PBO with
strategy-label randomization.

### `event_engine/queue.py`
**Purpose:** Message queue primitive for the event engine.

### `event_engine/exceptions.py`
**Purpose:** Typed exceptions raised by the engine.

### `examples/event_engine_analytics.py`
**Purpose:** Synthetic example that runs the analytics module end to
end and writes self-contained Plotly HTML.

**Run:** `.venv/bin/python -m examples.event_engine_analytics
--output-dir artifacts/analytics`.

## Cross-references

- `trading_bot/learning/experiments/controller.py::finalize_terminal`
  is the single owner of state restoration, persistence, event
  logging, and archival for every terminal outcome
  (KEPT/ROLLED_BACK/INCONCLUSIVE/ERROR/OFFLINE_REJECTED).
- `trading_bot/runtime/fill_transaction.py::build_*_transaction` is
  the only production boundary that threads canary_experiment_id and
  baseline quantity into the durable `orders` row.
- `trading_bot/swarm/engine.py` is no longer wired into the
  automated scan path; see AGENTS.md and the swarm-removal ADR for
  the rationale.
- `trading_bot/brokers/robinhood/` is MCP-only per AGENTS.md safety
  constraint; no direct auth.
