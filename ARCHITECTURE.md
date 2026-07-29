# System Architecture Overview

> Current as of 2026-07-29. When `AGENTS.md` changes in a way that
> affects the architecture, update this file in the same commit.

## Operational Atlas

A companion set of per-subsystem maps covers every operational file
with callers, side effects, tests, and status. See
[`docs/architecture/operational-atlas/`](docs/architecture/operational-atlas/)
for the entrypoint, runtime, data/signals/execution, persistence/
analytics/dashboard, burner/safety/monitoring, and learning/research/
integrations maps, plus the file-coverage index, verification matrix,
and remediation backlog.

## Data Flow

```
Market Data (Polygon → Alpaca → yfinance)
    ↓
Data Validation (V2.5 fail-fast: OHLC, price-jump, volume sanity)
    ↓
Indicators (EMA, SMA, RSI, ATR, MACD, Bollinger, VWAP)
    ↓
Strategy Engine
   - V2.5   (rule-based breakout, momentum, mean-reversion)
   - V3     (regime detection, confluence, counter-thesis, supermodel)
    ↓
Signal Generation (GREEN / YELLOW / NO_SIGNAL)
    ↓
Risk Management (ATR position sizing, ticker-allocation cap, portfolio heat)
    ↓
Order Submission (paper-only by default; Robinhood MCP-only via operator snapshot)
    ↓
Fill Persistence
   - PortfolioLedger (canonical, SQLite, `orders` table)
   - SQLAlchemy (`trades`, `positions` — separate, reconcilable projection)
    ↓
Dashboard / Reports / Alerts / Attribution
```

## Directory Structure

```
trading_bot/
├── cli/app.py              # Typer CLI (paper-trade, manage-positions,
│                           #   backtest, scan, paper-report,
│                           #   trade-attribution, cohort-resolver, …)
├── config/                 # Pydantic settings + YAML loader (loader
│                           #   forces live_trading_enabled=false)
├── data/                   # Market data, providers (Polygon, Alpaca,
│                           #   yfinance), indicators, EOD archive
├── brokers/                # BrokerAdapter ABC + Robinhood MCP boundary
│                           #   (legacy direct-auth code is gone)
├── strategy/               # V2.5, V3, supermodel, counter-thesis,
│                           #   trailing-stop, mean-reversion, breakout,
│                           #   gap-up screeners
├── risk/                   # ATR + capped position sizing, risk manager
├── execution/              # OrderRequest / FillResult / paper broker
│                           #   (test-only execution adapters were removed)
├── portfolio/              # PortfolioLedger, PortfolioState, Position
│                           #   model, JSONL result log
├── db/                     # SQLAlchemy session + Alembic-style ALTER
│                           #   TABLE migrations for additive columns
├── safety/                 # kill switch, circuit breaker,
│                           #   cohort-aware drawdown computation
├── monitoring/             # Health checks, performance alerts, fresh-
│                           #   scan / dashboard readiness (not webhooks;
│                           #   the Discord wrapper was removed)
├── analytics/              # Shared cohort-aware evaluation windows,
│                           #   timestamp normalisation, JSON-safe DTOs,
│                           #   paper-audit integrity check
├── runtime/                # Orchestrator + position_management (shared
│                           #   exit-priority evaluator) + position_exit
│                           #   + continuous_loop (loads runtime canary,
│                           #   ratchets trailing-stop)
├── learning/experiments/   # Experiment controller, offline replay,
│                           #   runtime canary, paired shadow harness
├── advisory/               # Manual opt-in learner (gated by
│                           #   advisory.enabled)
├── audit/                  # cohort integrity, paper-audit checks
├── backtest/               # Local EOD bar loader + chronological
│                           #   replays for offline validation
├── cli/app.py              # CLI commands (paper-trade, manage-positions,
│                           #   backtest, scan, paper-report, …)
└── events/                 # Event primitives (kept for future use;
                            #   not wired into production)

event_engine/               # Standalone installed research/backtest engine
├── events.py               # Frozen nanosecond event types
├── handlers.py             # Historical CSV/Parquet/in-memory data
├── portfolio.py            # Long/short account and margin accounting
├── execution.py            # Simulated exchange + market impact
├── strategy.py             # Strategy ABC + Bollinger reversion sample
├── prefilter.py            # Vectorized NumPy/pandas parameter sweep
├── engine.py               # Deterministic event-loop driver
└── analytics.py            # SQN, PSR/DSR, CPCV/PBO, reports, Plotly

ui/
└── dashboard/              # FastAPI + SSE + Jinja. Canonical rich
                            #   dashboard. The legacy runtime/HTML
                            #   dashboards were removed.

scripts/
├── auto-burn-in.sh         # Pre-market polling → discovery → scan →
│                           #   paper-trade → manage-positions
├── start-dashboard.sh      # Standalone dashboard launcher
├── daily-start.sh          # One-line commands per role
├── burn-in-monitor.sh      # Status snapshot
├── burn-in-weekly-review.sh
├── auto_bench_cron.py      # Optional alpha-factor benchmark
└── security-harden.sh      # Local-machine file-permission tighten
```

## Key Features

### Trading Strategies

- **V2.5** — Rule-based breakout, momentum continuation, mean reversion.
- **V3** — Regime detection, confluence scoring, counter-thesis guard, supermodel veto.

### Risk Controls

- Position cap: `risk.max_ticker_allocation_pct` (`0.20` in code and burn-in)
- Portfolio heat: `risk.max_portfolio_heat_pct` (`0.03` in code and burn-in)
- Hard per-ticker share cap: `risk.max_shares_per_position` (`50`)
- Burn-in entry guards: three daily orders, 2.0 minimum reward/risk,
  3% minimum stop distance, and a 30-minute ticker re-entry cooldown
- Burn-in circuit breakers: five consecutive losses or 10% cohort drawdown
- Daily-loss guard (resets by configured trading date)
- Paper-only by hardcode — `live_trading_enabled` forced `false` in `config/loader.py`

### Monitoring

- Real-time P&L tracking (paper)
- Performance metrics: win rate, profit factor, drawdown
- Cohort-aware dashboard (Today / Trade-cohort / Equity-cohort)
- Recent-Trades panel reads durable ledger fills (not decision log)
- `-doctor --burn-in` operator gate

### Tuning Experiments

See AGENTS.md **Tuning Experiment Controller** section.

### Runtime Canary

The runtime canary is a one-parameter paired-shadow experiment that
compares a live candidate policy against a frozen baseline without
submitting baseline orders to the paper broker. The canonical entry
points are:

- `trading_bot/learning/experiments/runtime_canary.py` — exports
  `begin_runtime_canary(settings, ledger)` and
  `finish_runtime_canary(context)`. The lifecycle derives the canonical
  experiment root from `<state_db_parent>/tuning_experiments`,
  constructs the controller, and reconciles durable order rows into
  the paired shadow ledgers via `PortfolioLedger.list_canary_order_rows`.
- `trading_bot/learning/experiments/controller.py` —
  `ExperimentController.finalize_terminal(state, status, reason)`
  is the single owner of state restoration, persistence, event
  logging, and archival for every terminal outcome
  (KEPT, ROLLED_BACK, INCONCLUSIVE, ERROR, OFFLINE_REJECTED).
  `activate_canary` persists `canary_starting_equity` BEFORE the
  candidate override bytes are activated.
- `trading_bot/learning/experiments/shadow.py` —
  `PairedShadowHarness` records BUY and SELL fills under a stable
  `operation_id` (the durable `FillResult.order_id`); duplicate IDs
  are silently dropped. `candidate_completed_trades()` /
  `baseline_completed_trades()` count full SELLs that close a
  ticker to zero; partial exits realize P&L without advancing the
  decision boundary.
- `trading_bot/runtime/fill_transaction.py` —
  `build_buy_transaction` and `build_sell_transaction` accept
  optional canary kwargs. The durable order row carries the
  `canary_experiment_id` (BUY also records `canary_baseline_quantity`)
  atomically with the fill. The runtime canary context is notified
  only AFTER the transaction succeeds, so a failed durable commit
  does not pollute the shadow ledgers.

Every production paper-trading entry point wraps begin/finish in
`try/finally`:

- `trading_bot/cli/app.py::paper_trade`
- `trading_bot/cli/app.py::_run_manage_positions_once`
- `trading_bot/runtime/continuous_loop.py::run_continuous_loop`

The controller's `_live_portfolio_is_flat` is fail-closed: a
never-initialized ledger is treated as flat, but any read/parse error
against an existing ledger returns False so a corrupted store cannot
silently enable canary activation.

### Quantitative Research Validation

- The root `event_engine` package is isolated from live paper execution.
- Performance analytics include R-multiples, SQN, annualized risk/return,
  drawdown depth/duration, PSR, and multiple-testing-adjusted DSR.
- CPCV uses horizon-aware purging/embargo, complete OOS paths, PBO ranks,
  and a dependence-preserving strategy-label randomization significance test.
- Markdown summaries and self-contained Plotly equity/drawdown HTML are
  available through `event_engine.analytics`.

## Testing

- 2,184 tests pass (network-free, monkeypatch `fetch_bars`; new additive regressions cover continuous CLI wiring, dashboard config routing, burn-in PIN_DIR handoff, and doctor PIN-aware state-dir routing).
- Run: `.venv/bin/python -m pytest -q`

## Configuration

Priority: Code defaults < `config.yaml` < Environment < CLI args

Critical settings:

- `live_trading_enabled: false` (HARDCODED)
- `app.state_db_path`: SQLite file for the ledger
- `app.timezone`: legacy naive timestamps are interpreted in this zone
- `paper.equity_evaluation_since`: cohort boundary for equity-risk evidence
- `paper.graduation_since`: cohort boundary for trade-quality evidence

## Cross-References

- **`AGENTS.md`** — canonical operational rules, entry-point, safety constraints.
- **`GETTING_STARTED.md`** — one-time setup.
- **`DAILY_STARTUP.md`** — morning pre-flight + start commands.
- **`QUICK_REFERENCE.md`** — one-page cheat sheet.
- **`docs/adr/001-exit-priority-order.md`** — ADR for the canonical
  exit priority (EOD > stop > target > time > counter-thesis > trailing).
