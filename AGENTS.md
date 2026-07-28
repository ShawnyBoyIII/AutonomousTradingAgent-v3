# AGENTS.md - Trading Bot

<!-- CODEGRAPH_START -->
## CodeGraph

This repo has `.codegraph/`. Use `codegraph explore "question"` or `codegraph node <symbol-or-file>` before grep/read when locating or understanding code.
<!-- CODEGRAPH_END -->

---

## Keeping AGENTS.md Current

AGENTS.md is a snapshot of the project's current operational reality, not a historical log.

- **Update this file in the same commit as any change it describes.** If a change touches the burner, risk knobs, dashboard, scripts, configs, kill switch, persistence, paper-performance analytics, or operational contracts, update the matching section here in the same commit so the file reflects the latest reality at all times.
- **No "temporary" sections.** Earlier versions of this file carried a `TEMPORARY OVERRIDES` block that listed the 2026-07-09 loose guardrail values. That block has been removed; if guardrails are loosened or tightened again, the new baseline belongs inline in this file as the documented truth, not as a "temporary" annotation.
- **Prefer updating comments to match the code** rather than leaving a stale doc that contradicts the implementation.
- **Live operational state, not intent.** Anything stated here is what the project currently does, not what it was supposed to do.

---
## Entry Point

**Always use the repo-local wrapper:**
```bash
./tradebot-local <command> [options]
./tradebot-local --config-path custom.yaml <command>
```

Never use bare `tradebot` on PATH — it may resolve to a stale global install.

**Config file selection:**
- `config.yaml` — default config for manual commands (`scan`, `paper-trade`, `portfolio`, etc.)
- `burn-in-config.yaml` — config for automated burn-in (`./scripts/auto-burn-in.sh`); enables V3 signals, different risk params
- `config.alpaca.yaml` — alternative config using Alpaca as market data provider
- Pass via `--config-path` to override: `./tradebot-local --config-path burn-in-config.yaml scan`

---

## Safety Constraints (Hard Rules)

1. **Paper-only by default** — `live_trading_enabled` forced `False` in `config/loader.py`
2. **Never modify tests** when fixing bugs — tests are source of truth
3. **All tests must be network-free** — monkeypatch `fetch_bars`, use `monkeypatch` fixtures
4. **Kill switch blocks all trading** — integrated at entry points before any logic
5. **No hardcoded credentials** — loader rejects config files with passwords/api keys
6. **Robinhood is MCP-only** — no direct auth; boundary subclasses `BrokerAdapter`, reads operator-synced JSON snapshots
7. **Python ≥ 3.11 required** — see `pyproject.toml` `requires-python`
8. **NumPy < 2** — pinned in `pyproject.toml`; newer versions break indicators
9. **Burn-in health is observable** — run `./tradebot-local doctor --burn-in` before market open to confirm heartbeat, dashboard, EOD watchdog, scan freshness, and DB are healthy.

---

## Paper Validation Goal

**Target**: Profit factor > 1.3 over 100 closed trades on paper with $100K starting capital.
Graduation evidence starts at `paper.graduation_since`; the burn-in cohort is
currently `2026-07-11T00:00:00+00:00`, after the 50-share cap was introduced.

Equity-risk evidence (drawdown, peak, return) starts at the dedicated
`paper.equity_evaluation_since` boundary. The current value is
`2026-07-15T22:29:45.354846-04:00`, the moment the live burn-in database
was reset to the $100K paper cohort. Pre-cohort equity rows (including
the legacy $1.27M peak) are excluded from cohort drawdown so a stale
peak cannot trip the circuit breaker.

- Burn-in runs daily via `./scripts/auto-burn-in.sh`
- Parallel signal mode is V3 + V2.5 consensus. The swarm engine is no longer in the automated scan/vote path — it remains as a manual/advisory tool (`./tradebot-local swarm`) only.
- 5% minimum stop distance on 5-minute bars (intraday noise protection)
- Fire-mode burn-in keeps a 25% ticker-allocation ceiling and a hard 50-share cap; default config uses 20%
- Confidence gates are advisory at PF < 0.8 after 50+ trades (logs alert, does not halt)
- The daily loss guard resets by configured trading date and sums that day's realized SELL P&L; cumulative historical P&L does not block a new session when dated SELL history is available
- Strategy tags recorded on all buys and sells for attribution
- Run `./tradebot-local paper-report` for the multi-dimensional P&L view (overall, by strategy, by hour, by ticker) — defaults to the trade-quality cohort (`paper.graduation_since`); pass `--since`/`--until` to override.
- Run `./tradebot-local trade-attribution` for the paired BUY/SELL roster
- Run `./tradebot-local graduation-check` for the configured cohort's 100-trade decision gate; explicit `--since`/`--until` values override the configured window
- Run `./tradebot-local drawdown` and `./tradebot-local risk-report` for cohort-bounded drawdown (uses `paper.equity_evaluation_since` when set, otherwise `paper.graduation_since`); fewer than two cohort snapshots → "Insufficient cohort evidence" instead of false halts
- Run `./tradebot-local doctor --burn-in` to verify heartbeat, dashboard, EOD watchdog, DB, and scan freshness before each cycle

**Decision gate**: When 100 closed trades are reached, review profit factor.
- PF > 1.3 → graduate to live trading consideration
- PF 0.8–1.3 → continue paper tuning
- PF < 0.8 → advisory alert logged; review decision-log.jsonl and run `./tradebot-local risk-report`

---

## Tuning Experiment Controller

The nightly burn-in step delegates tuning changes to a validated
experiment controller. Each experiment proposes exactly one allowlisted
parameter change:

- `supermodel.support_threshold` (step 0.05)
- `supermodel.block_threshold` (step 0.05)
- `supermodel.counter_veto_weight` (step 0.25)
- `strategy_tracker.full_allocation_rate` (step 0.05)

Validation pipeline:
1. Offline replay against the local EOD store with a 70/30 chronological split.
   The candidate must beat baseline by at least 0.10 PF, hold net P&L,
   keep drawdown within 5pp, and maintain ≥80% of baseline trade count.
2. 20-closed-trade paper canary with a paired shadow baseline running
   the same signals through an isolated ledger.
3. Keep only if the candidate still beats the shadow by ≥0.10 PF,
   net P&L > shadow, and drawdown within 5pp. Otherwise rollback.

State lives under `state/tuning_experiments/current.json` with an
append-only `events.jsonl`. Rollbacks restore the baseline bytes from
the experiment's snapshot. The plain `./tradebot-local tune` command
prints a notice and exits non-zero while an experiment is active.

---

## Testing

```bash
.venv/bin/python -m pytest -q          # full suite
.venv/bin/python -m pytest tests/test_kill_switch.py -v
.venv/bin/python -m pytest tests/eventengine_tests -q
```

**Requirements:**
- Tests are deterministic: monkeypatch market data, use `tmp_path` fixtures
- No real network calls
- Config: `pytest -ra --strict-markers` (pyproject.toml)

---

## Event-Driven Research Engine

The installed root package `event_engine/` is independent of the live
`trading_bot.events` surface. It provides immutable nanosecond events,
historical handlers, portfolio/execution simulation, vectorized parameter
screening, the event driver, and Stage 5 quantitative validation.

- `event_engine.analytics.PerformanceAnalytics` owns R-multiples, SQN,
  CAGR, volatility, Sortino, Calmar, and drawdown metrics.
- `DSRDiagnostics` reports PSR/DSR on per-observation Sharpe values; trial
  Sharpe inputs must use the same scale as the return sample.
- `CombinatorialPurgedCV` purges overlapping event horizons, embargoes after
  label end-times, builds complete CPCV OOS paths, and reports PBO with a
  strategy-label randomization p-value.
- Run the synthetic example with `.venv/bin/python -m
  examples.event_engine_analytics --output-dir artifacts/analytics`.

---

## Configuration

**Critical defaults (config.yaml):**
```yaml
app:
  live_trading_enabled: false  # Always false, enforced in code
  state_db_path: state/burn_in.db

risk:
  max_risk_per_trade_pct: 0.01
  max_ticker_allocation_pct: 0.25  # Fire-mode burn-in; default config uses 20%
  max_portfolio_heat_pct: 0.10     # Fire-mode burn-in; default config uses 3%
  max_shares_per_position: 50      # Hard per-ticker share cap

market_data:
  validate_data: true  # V2.5: fail-fast

paper:
  graduation_since: "2026-07-11T00:00:00+00:00"  # trade-quality cohort
  equity_evaluation_since: "2026-07-15T22:29:45.354846-04:00"  # equity-risk cohort

app:
  dashboard_port: 8000  # overridden by DASHBOARD_PORT env var
```

**Quirks:**
- Paths resolved relative to config file location
- Credentials via environment variables or `.env` only (`.env` is gitignored)
- `state/burn_in.db` for burn-in, `state/trading_bot.db` for manual commands

---

## Cohort-Aware Reporting & Dashboard

Three evaluation windows are computed from a single source
(`trading_bot/analytics/evaluation_windows.py`) and shared by the CLI
reports and the dashboard:

- **Today**: realized SELL P&L since the configured `app.timezone`
  midnight, used as the hero's "Today · Realized P&L" KPI.
- **Trade Cohort**: realized SELL P&L since `paper.graduation_since`;
  default for `paper-report`, `trade-attribution`, and
  `graduation-check`.
- **Equity Cohort**: starting/current equity, peak, drawdown, and
  return since `paper.equity_evaluation_since` (or
  `graduation_since` fallback); gates the dashboard's drawdown and
  starting-equity baseline.

Each window returns a status envelope (`ready` / `empty` /
`insufficient` / `unconfigured` / `error`) plus JSON-safe metrics.
Legacy naive `filled_at` rows are interpreted in `app.timezone`,
not silent UTC, so cohort boundaries align with the operator's
configured trading day.

The canonical live dashboard is `ui/dashboard/main.py` (FastAPI +
SSE + Jinja). The CLI `serve` command launches it via uvicorn on
`app.dashboard_port` (8000 in the default config), overridable via
`--port`. The burn-in sidecar launches the same application through
`scripts/start-dashboard.sh` on port 8080 unless overridden. The
older `runtime/dashboard.py` legacy dashboard and the static HTML
generator have been removed. The CLI `dashboard` command remains as
a deprecation no-op alias that points operators at `serve`.

**Doctor port discovery.** `doctor --burn-in` resolves the dashboard
port via `trading_bot/cli/app.py::resolve_dashboard_port` with this
precedence: `DASHBOARD_PORT` env var → `state/burn_in/dashboard.port`
(written by the burner when the sidecar starts) → `settings.app.dashboard_port`
→ 8000. The port file is the bridge that lets a manual operator run
`./tradebot-local doctor --burn-in` from outside the burner and
discover the burner's actual sidecar port without exporting env vars.
The file is removed on `stop_dashboard` so a stale file from a dead
burner is overwritten on the next launch.

**Doctor `--burn-in` config routing.** `./tradebot-local doctor --burn-in`
(no `--config-path`) re-loads settings from `burn-in-config.yaml`
instead of `config.yaml`. The burner itself always uses
`burn-in-config.yaml`, and `config.yaml`'s legacy
`state/scan_results.json` is no longer written by anything. Earlier
operators had to pass `--config-path burn-in-config.yaml` to get
accurate scan-freshness results; now the doctor self-routes when
`--burn-in` is the entry point (and respects an explicit
`--config-path` override as a higher precedence).

**Doctor market-data freshness.** `check_market_data_freshness` reads
`state/market_data_cache.db` (`MarketDataCache`, the canonical store)
first, falling back to the legacy `market_data` table in
`state/burn_in.db` only if the cache is missing. Earlier the check
queried the vestigial `market_data` table that no production code
populates, so a busy burner with fresh intraday bars still reported
`market_data_freshness: WARN  no market data yet`. The runner wires
the cache path via `cache_db_path` (defaults to
`<state_db_path parent>/market_data_cache.db`).

**EOD fetch target date.** `eod-fetch` defaults to the previous
**trading** day in America/New_York (`_previous_trading_day` helper),
so Monday-evening fetches skip Sunday and don't 404 on the empty S3
partition. The burner no longer passes `--date "$(date -v -1d ...)"`;
it relies on the CLI default so weekend and DST transitions don't
slip a calendar-yesterday target into the S3 request.

Dashboard endpoints:

- `GET /api/portfolio` — ledger + risk snapshot
- `GET /api/evaluation-windows` — full three-window payload
- `GET /api/trades` — recent durable fill rows from the configured
  `PortfolioLedger.orders` table, newest first
- `GET /api/health` — overall and per-check statuses use lowercase
  `ok` / `critical`, with structured detail
- `GET /api/stream` — SSE includes the same payload every 5 seconds

The hero's "Since <boundary>" caption is driven by the equity
cohort's starting equity; the hardcoded `$350,000` placeholder is
gone. Empty cohort evidence never reports `0% drawdown`; it reports
`Insufficient evidence`.

---

## Architecture

```
trading_bot/
├── cli/app.py           # Typer CLI commands
├── config/              # Settings, loader (YAML → Pydantic)
├── data/                # Market data, validation, indicators
│   ├── market_data.py   # fetch_bars() - monkeypatch this
│   └── validation.py    # V2.5: price/OHLC/volume validation
├── execution/           # Order management
│   └── paper_broker.py  # Simulated fills with slippage/fees
├── brokers/robinhood/   # MCP boundary + legacy code
├── portfolio/           # Ledger, P&L tracking
├── risk/                # Position sizing (ATR + cap)
├── strategy/            # Signals, V3 counter-thesis
├── safety/kill_switch.py
└── runtime/orchestrator.py
```

**Data flow:**
1. `scan` → checks universe for GREEN signals
2. `paper-trade` → generates orders, simulates fills
3. `manage-positions` → checks stops, targets, EOD exits
4. All entry points check kill switch first

**Ledger trade lifecycle:** There is no single `record_closed_trade` method.
A trade is recorded in two steps: `record_fill()` on BUY/SELL (writes to
`orders` table) then `update_trade_exit()` on SELL (updates P&L fields).
Cross-table reporting reads both `orders` and `trades` tables separately.
Position cost basis uses the actual slipped BUY fill. New SELL `pnl` values are
net of BUY and SELL fees, with BUY fees allocated proportionally across partial
exits; cumulative portfolio P&L does not double-count the BUY fee charged when
the position opened. The `2026-07-11` paper cohort's persisted SELL rows were
reconciled to this fill-to-fill net convention; older legacy rows were not.

---

## Common Commands

```bash
# Daily startup workflow
./scripts/daily-start.sh

# Health check
./tradebot-local doctor
./tradebot-local doctor --burn-in            # burn-in reliability report
./tradebot-local doctor --burn-in --json     # machine-readable

# Scan and trade
./tradebot-local scan --symbols SPY,AAPL --why --summary
./tradebot-local paper-trade --symbols AAPL
./tradebot-local manage-positions
./tradebot-local portfolio
./tradebot-local performance --daily

# Kill switch
./tradebot-local kill-switch --status|--halt|--resume

# Backtest
./tradebot-local backtest --symbols AAPL --start 2025-01-01 --end 2025-06-01

# Universe building
./tradebot-local build-universe
./tradebot-local scan-universe --summary

# Dashboard
./tradebot-local --config-path burn-in-config.yaml serve --port 8000

# V3: Robinhood MCP (read-only)
./tradebot-local robinhood-status
./tradebot-local sync-positions

# Burn-in automation
./scripts/burnin-launcher.sh          # PINNED launcher — use this for unattended runs
./scripts/auto-burn-in.sh              # auto-starts dashboard sidecar on :8080
                                       # AUTO_DASHBOARD=false to opt out
                                       # DASHBOARD_PORT=N to change port
./scripts/burn-in-monitor.sh
./scripts/burn-in-weekly-review.sh
tail -f logs/burn_in/decision-log.jsonl

# Tuning and reporting
./tradebot-local tune --dry-run
./tradebot-local db-features --summary
./tradebot-local trade-attribution
./tradebot-local risk-report

# Tuning experiment controller
./tradebot-local tune-experiment propose
./tradebot-local tune-experiment status
./tradebot-local tune-experiment evaluate
./tradebot-local tune-experiment rollback --reason "operator note"
./tradebot-local tune-experiment status --json

# Tuning experiments persist to state/tuning_experiments/; the shadow baseline
# canary appends fills/equity lines to <artifacts_dir>/shadow-fills.jsonl and
# shadow-equity.jsonl (JSONL, one record per line).

## Runtime Canary Contract

Live paper trading during an experiment's `CANARY` phase mirrors every
BUY and SELL into paired shadow ledgers via `RuntimeCanaryContext`. The
production lifecycle is two calls: `begin_runtime_canary(settings, ledger)`
returns a context (or `None` when no canary is active) and
`finish_runtime_canary(context)` snapshots metrics once per command or
continuous-loop cycle. Both calls are wrapped in `try/finally` in every
entry point (`paper-trade`, `manage-positions`, `run_continuous_loop`)
so paired metrics persist regardless of how the cycle ends.

**Canonical store.** The lifecycle derives the experiment root from
`<settings.app.state_db_path parent>/tuning_experiments`. Production
callers cannot accidentally supply an injected store or controller; tests
use a private `_build_canary_context_with_deps` helper for dependency
injection. Malformed or inaccessible active state raises
`RuntimeCanaryLifecycleError` so corruption is observable rather than
silently equated with "no canary".

**Allowlist.** The runtime canary supports exactly one parameter today:
`supermodel.range_bound_trend_caution_multiplier` with `0 < candidate <= 1`
and `0 < baseline <= 1`. Any other field returns `None` from
`begin_runtime_canary` so unsupported experiments never enter runtime
mirroring. Add support for new parameters with a fresh spec revision
plus fixtures; do not extend the allowlist in ad-hoc commits.

**Durable canary metadata.** Each durable `orders` row gains two
additive nullable columns: `canary_experiment_id` (set on every fill
during an active canary) and `canary_baseline_quantity` (set on BUY
rows with the pre-policy baseline size). SELL rows record the
experiment id; their baseline quantity is derived from the paired
position fraction during reconciliation.

**Idempotent shadow recording.** Every shadow fill carries the durable
`FillResult.order_id`. Both the in-memory ledger and the JSONL replay
drop duplicate non-empty IDs. Re-recording a durable fill that crashed
between the SQLite commit and the JSONL append is a no-op, so lifecycle
reconciliation can backfill missing rows safely.

**Completed-position gate.** The 20-trade decision boundary counts
completed positions (a SELL that drives a ticker to zero), not partial
SELL fills. Profit factor and net P&L still include each realized
partial-exit component; only the completion count gates the decision.
A paired-ledger divergence (`candidate_completed_trades` ≠
`baseline_completed_trades`) immediately invalidates the canary.

**Activation gate.** Before promoting `PROPOSED → CANARY`, the
controller verifies the live portfolio is flat and the ledger is
readable. A non-flat or unreadable ledger produces `INCONCLUSIVE`
with the matching reason, and the candidate bytes are never written.
Starting equity is persisted (`canary_starting_equity_recorded`) BEFORE
candidate bytes are activated so restarts rebuild the harness against
the same baseline.

**Restart safety.** The harness reads `state.canary_starting_equity` on
every load so a crashed process restarts against the same baseline cash.
On lifecycle start, durable order rows for the active experiment are
reconciled into the paired shadow ledgers via
`PortfolioLedger.list_canary_order_rows`, so any JSONL gaps from a
mid-fill crash are backfilled exactly once.

**Terminal finalization.** Every CANARY outcome (KEPT, ROLLED_BACK,
INCONCLUSIVE, ERROR, OFFLINE_REJECTED) routes through one
`ExperimentController.finalize_terminal(state, status, reason)`. It
restores baseline overrides (except for KEPT), persists the state,
logs the event, and archives the experiment. Restoration failure
raises `RuntimeCanaryLifecycleError` and leaves the state in the
active store so an operator can investigate.

**Pass/fail threshold.** The runtime canary runs alongside the existing
experiment rules in `_decide`. The runtime metrics feed the same
`candidate_metrics` vs `shadow_metrics` comparison; candidates that fail
the existing PF / net-P&L / drawdown gates are auto-rolled back.
After 10 market sessions without 20 completed positions the experiment
becomes `INCONCLUSIVE` and is archived.

Verified by `tests/test_runtime_canary_*.py` covering the harness,
controller guards, context seam, BUY wiring, SELL wiring, CLI
lifecycle, end-to-end behavior, and idempotency/reconciliation.


# Advisory learner (paper-only; opt-in via advisory.enabled)
./tradebot-local advisory-learn
./tradebot-local advisory-learn --daily-report
./tradebot-local advisory-report --markdown
./tradebot-local advisory-report --json
```

---

## Burn-In Runtime Pin (2026-07-24)

The auto-burn-in script is a long-running resident process. While it
holds the burner PID, the worktree's `git switch` may change the source
code that subsequent `sh ./tradebot-local ...` subprocess invocations
re-import — silently poisoning the running burner with a foreign
revision. The 2026-07-24 false-drawdown halt was caused by exactly this:
while the burner was running, the worktree was switched to a legacy V2
marker branch; the next paper-trade subprocess re-imported the V2
`circuit_breaker`, read the legacy equity cohort, and halted at the
exact 44.9296% drawdown — while a fresh CLI on the same ledger reported
1.78%.

**The boundary:** `scripts/burnin-launcher.sh` captures an immutable
snapshot of `HEAD` via `git archive HEAD | tar -x -C
.burnin_pin/<head_sha>/` and `exec`s `scripts/auto-burn-in.sh` from the
snapshot root with `PIN_DIR` exported. Every Python subprocess then
resolves through the snapshot's wrapper, not the live mutable worktree.

- **Always use `./scripts/burnin-launcher.sh` for unattended runs.**
  `./scripts/auto-burn-in.sh` is still callable for ad-hoc / debugging
  scenarios where the worktree is stable, but the launcher is the
  pinned contract.
- The snapshot lives under `.burnin_pin/<head_sha>/`. SHA is recorded
  in `state/burn_in_health/burn_in.pin` so the doctor can confirm the
  burner is operating from the expected snapshot.
- Doctor `--burn-in` reports the pin SHA and the fingerprint of pinned
  paths. A fingerprint mismatch means the live worktree edited
  `tradebot-local`, `scripts/auto-burn-in.sh`, `trading_bot/runtime/burnin_pin.py`,
  or `scripts/burnin-launcher.sh` mid-session — restart the burner to
  re-snapshot.
- Manual `./tradebot-local ...` invocations keep working against the
  live worktree when `PIN_DIR` is unset, so the operator's CLI workflow
  is unaffected.

---

## V3 Strategy Layer (Feature Flag)

**Enable in config:**
```yaml
strategy:
  use_v3_signals: true      # Default: false
  risk_tolerance: medium
  min_confidence: medium

counter_thesis:
  enabled: true             # Default: false
  block_on_severity: high
  aggregate_block_threshold: 0.6
  exit_on_block: true
```

**Behavior:**
- `scan --why` surfaces `v3_total_score`, `v3_confidence`, `v3_regime`
- Counter-thesis vetoes trades or scales position by `confidence_multiplier`
- `manage-positions` exits when thesis broken (before trailing stop)
- Backtest runner evaluates counter-thesis on each bar (no network)

**Design:** `fetch_counter_thesis_context` is the only network-touching entry; all `_check_*` are pure functions of `(context, settings)`.

---

## Exit Priority (ADR-001)

1. **EOD exit** (highest - always exit before close)
2. **Stop loss**
3. **Profit target**
4. **Time-based exit** (stale positions, configurable via `time_exit_minutes`)
5. **Counter-thesis exit** (V3: thesis broken)
6. **Trailing stop** (lowest)

**Canonical evaluator and closed-trade exit reasons:**

- `eod_exit` — EOD window exit
- `stop_loss` — stop-loss hit
- `profit_target` — full target hit (or partial-take-profit branch)
- `time_exit_{minutes}m` — held past `time_exit_minutes`
- `counter_thesis` — V3 counter-thesis block
- `trailing_stop` — trailing stop hit
- `no_exit` — evaluator returned without firing (only used internally)

The shared `evaluate_exit_priority()` helper in
`trading_bot/runtime/position_management.py` produces these canonical
reasons for both the CLI `manage-positions` command and the continuous
loop. The CLI preserves its outward decision-log, event, strategy-tracker,
and human-readable reason contract (`eod`, `stop`, `target`, and
`counter-thesis`) while passing the canonical reason separately to the SQL
closed-trade `exit_reason` field. Partial exits retain `target_partial` and
do not close the SQL trade. The continuous loop uses canonical outward
reasons.

---

## Data Validation (V2.5)

All market data validated before use:
1. Price sanity: 0 < price < 10x jump from previous
2. OHLC coherence: high ≥ close ≥ low
3. Volume sanity: reasonable levels

Fail-fast: stops on first validation error.

---

## Branch Convention

- Use `v2/main` as the local base when reviewing session-specific deltas; `origin/main` is older and PR diffs there can include a large pre-existing stack.
- `git archive HEAD | tar -x -C <tmp-dir>` plus an import smoke test — verifies a branch is self-contained; the live worktree can mask missing tracked files with untracked local dependencies.

---

## Session Gotchas

- When calling `trading_bot.runtime.position_exit` helpers outside CLI entrypoints, pass the active `settings` object explicitly so exit persistence uses the intended DB/log paths.
- Intraday backtests are causal: signals receive only completed prior-day context, approved setups fill at the next observed bar open, next opens at or beyond the original stop/target cancel the setup, exits are evaluated one bar at a time, same-bar stop/target collisions choose the stop, and configured paper fees/slippage apply. When `app.signal_mode=parallel`, replay and paper mode share the V3 + V2.5 consensus, counter-thesis, supermodel veto, and one-source half-sizing path. Portfolio-wide correlation, sector exposure, cooldown, and adaptive strategy-tracker state remain paper-runtime concerns; only the configured paper cohort counts toward graduation.
- New paper fills are persisted as timezone-aware UTC timestamps. Legacy order rows before the `2026-07-11` cohort may contain naive America/New_York wall time and must not be mixed into UTC-window conclusions.
- `list_equity_history()` retains legacy oldest-first semantics; monitoring and audits must use `list_recent_equity_history()` when requesting a bounded recent window.
- `trading_bot.data.providers.registry` is the source of truth for market-data capabilities, network-free credential readiness, and intraday fallback priority. Add or change a provider there in the same commit as its adapter; unsupported intervals are skipped before provider construction.
- `./tradebot-local tune` writes `state/tuning_overrides.yaml`; loader applies only allowlisted supermodel + strategy-tracker fields and still forces `live_trading_enabled=false`
- Swarm worker votes (when running the manual `./tradebot-local swarm` command) are logged to `logs/worker_votes.jsonl`; use this file for per-worker weight tuning
- Decision-log and paper-trade rows preserve compact supermodel evidence even on rejects and `NO_SIGNAL`; use `paper-report`, `trade-attribution`, and `db-features` for paper review before adding new logging.

---

## Key Files

- `trading_bot/cli/app.py` - CLI commands
- `trading_bot/config/loader.py` - Config loading + safety enforcement
- `trading_bot/runtime/orchestrator.py` - V2.5 + V3 signal paths
- `trading_bot/strategy/counter_thesis.py` - Counter-thesis engine
- `trading_bot/strategy/strategy_selector.py` - Regime + confluence
- `trading_bot/risk/position_sizer.py` - ATR sizing logic
- `trading_bot/advisory/learner.py` - Advisory learner
- `scripts/auto-burn-in.sh` - Automated burn-in loop
- `docs/V2_5_PHASE_D_BURN_IN_GUIDE.md` - Operational guide

---

## Troubleshooting

**NumPy 2 incompatibility:**
```bash
.venv/bin/python -m pip install --force-reinstall "numpy<2" "pandas>=2.2" "pyarrow" "numexpr" "bottleneck"
```

**Tests failing:**
```bash
.venv/bin/python -m pytest tests/test_kill_switch.py::test_kill_switch_status -v
```

**Position sizes too small:**
- Verify `max_ticker_allocation_pct` (burn-in: 0.15, default: 0.20)
- Check `max_portfolio_heat_pct` (blocks at 3% unrealized loss)
- Review ATR multiplier settings
