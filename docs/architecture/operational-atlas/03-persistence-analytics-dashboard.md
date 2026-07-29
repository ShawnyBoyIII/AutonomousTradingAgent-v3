# Phase 3 — Persistence, Analytics, and Dashboard

> 2026-07-29 snapshot at HEAD `62d178b`. All subsystems exercised by
> the burner's analytics loop or via read-only SQLite inspection.
> No new defects found in this audit; pending hypotheses captured in
> the remediation backlog.

## Persistence

### `trading_bot/portfolio/ledger.py`
**Purpose:** Canonical `PortfolioLedger`. Owns the `orders` table
writes via `record_fill()`, `update_trade_exit()`, equity snapshots,
kill-switch persistence, position book.

**Tests:** `tests/test_ledger.py` family. All pass.

**Live evidence:** ledger at `state/burn_in.db` initialized with
$100,000 starting cash, zero orders, zero positions, zero cooldowns.

### `trading_bot/db/session.py`
**Purpose:** SQLAlchemy session, `make_session_factory`, `init_db`,
`get_session`. Idempotent table creation; `init_db` is safe to call
against a freshly initialized `state/burn_in.db`.

**Tests:** `tests/test_db_session.py` family. All pass.

### `trading_bot/db/models.py`
**Purpose:** SQLAlchemy models for `Trade`, `Position`,
`PortfolioSnapshot`, `ScanResult`, `ScanFeature`, `ModelPrediction`,
`MarketDataRow`, `Event`.

### `trading_bot/db/repositories/`
**Purpose:** Repository pattern for each model. Each module exposes
typed query helpers used by analytics and dashboard APIs.

- `trading_bot/db/repositories/trades.py`
- `trading_bot/db/repositories/positions.py`
- `trading_bot/db/repositories/portfolio_snapshots.py`
- `trading_bot/db/repositories/scan_results.py`
- `trading_bot/db/repositories/scan_features.py`
- `trading_bot/db/repositories/model_predictions.py`
- `trading_bot/db/repositories/market_data.py`
- `trading_bot/db/repositories/events.py`

### `trading_bot/runtime/mark_to_market.py`
**Purpose:** Mark-to-market helper for unrealized P&L. Consumed by
`/api/portfolio` and `/api/health`.

### `trading_bot/runtime/snapshots.py`
**Purpose:** Portfolio snapshot scheduling. Invoked at every paper-trade
cycle to record an equity history row.

### `trading_bot/portfolio/performance.py`
**Purpose:** Heat + drawdown + per-strategy performance aggregations.
Consumed by `paper-report`, `risk-report`, and dashboard.

## Analytics

### `trading_bot/analytics/evaluation_windows.py`
**Purpose:** Single source of truth for the three evaluation windows
(`today`, `trade cohort`, `equity cohort`). Returns status envelopes
(`ready`/`empty`/`insufficient`/`unconfigured`/`error`) plus
JSON-safe metrics.

**Tests:** `tests/test_evaluation_windows.py`. All pass.

### `trading_bot/analytics/paper_performance.py`
**Purpose:** Multi-dimensional P&L aggregations: overall, by strategy,
by hour, by ticker.

**Tests:** `tests/test_paper_performance.py`. All pass.

### `trading_bot/reports/burn_in_analytics.py`
**Purpose:** `compute_burn_in_report` and `format_report` for the
`burn-in-report` CLI.

**Tests:** `tests/test_burn_in_analytics.py`. All pass.

### `trading_bot/reports/summaries.py`
**Purpose:** Trade/portfolio summary helpers.

### `trading_bot/reports/exporters.py`
**Purpose:** CSV/JSON exporters for the same summary data.

## CLI commands (analytics)

| Command | Tests | Notes |
| --- | --- | --- |
| `paper-report` | `tests/test_paper_report.py` | defaults to trade cohort; honors `--since/--until` |
| `trade-attribution` | `tests/test_trade_attribution.py` | paired BUY/SELL roster |
| `graduation-check` | `tests/test_graduation_check.py` | 100-trade decision gate |
| `drawdown` | `tests/test_drawdown.py` | cohort-bounded drawdown |
| `risk-report` | `tests/test_risk_report.py` | cohort-bounded risk summary |
| `burn-in-report` | `tests/test_burn_in_analytics.py` | burn-in operational report |
| `db-features` | `tests/test_db_features.py` | DB feature summary |
| `performance --daily` | `tests/test_performance.py` | daily P&L summary |

## Dashboard

### `ui/dashboard/main.py`
**Purpose:** Canonical FastAPI dashboard. Module-level
`DashboardState()` reads settings via `load_settings()` (now honors
`CONFIG_PATH`). Routes:

- `GET /` (HTML) — Jinja-rendered dashboard shell.
- `GET /api/portfolio` — ledger + risk snapshot.
- `GET /api/evaluation-windows` — three-window payload.
- `GET /api/trades` — recent durable fill rows.
- `GET /api/health` — health report.
- `GET /api/closed-trades` — paired closed-trade roster.
- `GET /api/stream` — SSE every 5s.

### `ui/dashboard/static/css/dashboard.css`
**Purpose:** Styling for the dashboard template.

### `ui/dashboard/static/js/dashboard.js`
**Purpose:** Live SSE consumer; updates hero KPIs and the trade book
without page reload.

### `ui/dashboard/templates/dashboard.html`
**Purpose:** Jinja template for the dashboard shell.

**Live evidence:** Burner sidecar at `:8080`, `/api/health` returns
`status: ok` with 5 sub-checks all `ok`.

## Cross-references

- `trading_bot/analytics/evaluation_windows.py` is the source of truth
  shared by CLI reports and dashboard `/api/evaluation-windows`.
- `trading_bot/portfolio/ledger.py::PortfolioLedger` creates its own
  base ledger tables; `trading_bot/db/session.py::init_db` creates
  the SQLAlchemy projection tables. Fresh DB setup requires both
  (memory #48).
- `ui/dashboard/main.py::DashboardState.__init__` calls
  `load_settings()`, which honors the three-tier config precedence
  established by commit `82d1700`.
- `scripts/start-dashboard.sh` exports `CONFIG_PATH` before
  `uvicorn` invocation; `trading_bot/cli/app.py::serve` re-exports the
  resolved absolute path before `uvicorn.run` to ensure the
  module-level `DashboardState` reads the same config (commits
  `82d1700` and the dashboard config routing test in
  `tests/test_dashboard_config_routing.py`).

## Open hypotheses

- **Fill persistence atomicity.** A trade is recorded in two steps
  (`record_fill` then `update_trade_exit`). A crash between the two
  writes can leave an `orders` row without a paired `trades` row.
  Documented in the remediation backlog.
- **Empty cohort evidence.** `compute_cohort_drawdown` returns
  `insufficient` for fewer than two cohort snapshots; reports correctly
  render `Insufficient evidence` instead of `0% drawdown`. Confirmed
  during phase 4 audit; recorded as a contract in
  `AGENTS.md` "Cohort-Aware Reporting & Dashboard".
