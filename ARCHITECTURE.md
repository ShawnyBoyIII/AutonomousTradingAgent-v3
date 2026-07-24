# System Architecture Overview

> Current as of 2026-07-24. When `AGENTS.md` changes in a way that
> affects the architecture, update this file in the same commit.

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

- Position cap: `risk.max_ticker_allocation_pct` (default `0.20`, fire-mode burn-in `0.25`)
- Portfolio heat: `risk.max_portfolio_heat_pct` (default `0.03`, fire-mode burn-in `0.10`)
- Hard per-ticker share cap: `risk.max_shares_per_position` (`50` in fire-mode)
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

### Quantitative Research Validation

- The root `event_engine` package is isolated from live paper execution.
- Performance analytics include R-multiples, SQN, annualized risk/return,
  drawdown depth/duration, PSR, and multiple-testing-adjusted DSR.
- CPCV uses horizon-aware purging/embargo, complete OOS paths, PBO ranks,
  and a dependence-preserving strategy-label randomization significance test.
- Markdown summaries and self-contained Plotly equity/drawdown HTML are
  available through `event_engine.analytics`.

## Testing

- 2,099 tests pass (network-free, monkeypatch `fetch_bars`); one pre-existing assertion mismatch in `test_run_symbol_backtest_replays_multiple_trade_cycles` remains.
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
