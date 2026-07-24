# V1 App Overview

## What This App Does

Autonomous Trading Agent is a local, CLI-first stock and ETF research bot.

V1 can:

- Scan symbols for long trade candidates.
- Explain why each symbol is approved or rejected.
- Mark approved candidates as `GREEN` or `YELLOW`.
- Paper trade only fresh `GREEN` signals.
- Track local paper portfolio state.
- Produce local reports.
- Replay historical data with a simple backtest.
- Write local JSON snapshots for runtime integrations.
- Serve the canonical FastAPI + SSE + Jinja dashboard at `ui/dashboard/main.py`.
- Check local readiness without fetching market data.

V1 is paper-only by default. It does not place live broker orders.

## Main Commands

```bash
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY,NVDA,QQQ --why --summary
sh ./tradebot-local paper-trade --symbols SPY --dry-run
sh ./tradebot-local paper-trade --symbols SPY
sh ./tradebot-local backtest --symbols AAPL,MSFT,SPY,NVDA,QQQ --start 2026-05-01 --end 2026-06-17
sh ./tradebot-local portfolio
sh ./tradebot-local report
sh ./tradebot-local serve
sh ./tradebot-local doctor
sh ./tradebot-local manage-positions
```

Use `tradebot-local` so the app runs from this repo's `.venv`, not a stale global install.

## Scan Logic

Scan uses daily trend plus intraday setup.

Daily filter:

- Price must be in bullish daily regime.
- If daily regime fails, result is `NO_SIGNAL reason=daily regime not bullish`.

Intraday setup:

- Breakout: close clears recent range high with volume support.
- Momentum continuation: price is moving up and closing strong enough inside the candle.
- If intraday setup fails, result is `NO_SIGNAL reason=no intraday setup`.

`--why` adds gate values:

- `daily_close`
- `ema_20`
- `sma_50`
- `intraday_close`
- `range_high`
- `volume`
- `volume_avg`
- `volume_ratio`

`--summary` adds one totals line with symbols, approved, green, yellow, rejected, no-signal, and error counts.

## Signal Quality

Approved signals get quality label.

`GREEN` means:

- Intraday close is above recent range high.
- `volume_ratio >= 1.00`.

`YELLOW` means:

- Signal passed strategy rules, but confirmation is weaker.
- Example: momentum setup below range high or volume below average.

## Paper Trading Guardrails

Paper trading is stricter than scan.

Use `--dry-run` to preview a fill without writing orders or portfolio state.

Paper trade rejects:

- No signal.
- Stale market data.
- `YELLOW` signals.
- Duplicate open ticker.
- Daily order limit.
- Daily loss limit.
- Risk-manager rejection.
- Insufficient cash.
- Broker/order rejection.

Only fresh `GREEN` signals can fill.

## State And Snapshots

Runtime files are local and ignored by git.

Generated under `state/`:

- `scan_results.json`
- `portfolio_summary.json`
- `dashboard_summary.json`
- `backtest_summary.json`
- `trading_bot.db`

Generated under `logs/`:

- `decision-log.jsonl`

These files are replaceable local runtime artifacts.

## Current V1 Status

V1 is good as local trading research and paper-trading base.

Strengths:

- Clean local runner.
- Explainable scan output.
- Conservative paper-trade guardrails.
- JSON snapshots ready for future UI.
- Tests cover core CLI, strategy, risk, paper broker, portfolio, and backtest.

Limits:

- Market data can be stale or imperfect.
- Strategy is simple.
- Backtest is useful but not institution-grade.
- No live trading.
- The current application includes the live monitoring dashboard at `ui/dashboard/main.py`.
- No macro or breadth model yet.

## Safety Position

V1 should not auto-trade real money.

Use it for:

- Research.
- Signal review.
- Paper trading.
- Dashboard/UI foundation.

Do not use it for:

- Live execution.
- Unsupervised trading.
- Large capital decisions.

## Next Best Work

Completed from follow-up priority:

- Daily loss limit and daily order limit.
- Paper-trade dry-run preview.
- Live FastAPI dashboard with SSE updates.
- Scan summary table.

Future work priority:

1. Keep paper trading stable with real usage feedback.

Reason:

- Daily loss/order limits prevent rogue local loops from creating too many simulated orders.
- Dry-run preview gives final sanity check before writing fills to SQLite.
- The live dashboard makes paper portfolio debugging easier than terminal-only inspection.

Launch it with `./tradebot-local serve` (default config port 8000) or
`./scripts/start-dashboard.sh` (port 8080 by default). Both launchers run
the same `ui/dashboard/main.py` application.

Later:

- Add scan summary table.

## V2 Completion Summary

V2 is now complete and production-ready for paper trading with full position lifecycle management.

### Position Management Lifecycle

**Commands:**
```bash
# Single-shot position check
sh ./tradebot-local manage-positions

# Continuous daemon with 60s interval
sh ./tradebot-local run-manager --interval 60

# Tight loop (no sleep between iterations)
sh ./tradebot-local run-manager --interval 0
```

**Exit Priority Order:** EOD → Stop → Target → Trail

1. **EOD Exit** — Liquidates all positions at configured end-of-session time (default 15:55 ET) to avoid overnight risk
2. **Stop Loss** — Hard stop to limit downside
3. **Profit Target** — Take profit at predetermined level
4. **Trailing Stop** — Dynamic stop that ratchets up only, never down

See `docs/adr/001-exit-priority-order.md` for full rationale.

### New V2 Features

**Latency Gate (Chunk A):**
- Configurable `max_data_age_hours` (default 72 hours)
- `manage-positions` skips all actions if market data is stale
- Reports `skipped=N` in summary line for observability
- Pure helper functions in `trading_bot/runtime/latency.py`

**Slippage & Fees (Chunk B):**
- `PaperSettings` in config: `fee_per_order` (default $1.00), `slippage_bps` (default 0)
- Applied on every paper broker fill
- Makes simulation realistic — strategy must prove edge survives friction

**SQLite Resilience (Chunk C):**
- `busy_timeout` pragma (default 5000ms)
- Bounded retry on lock errors (3 attempts, exponential backoff)
- Clean handling when scanner and manager run concurrently
- Full test coverage in `tests/test_ledger_locks.py`

**Continuous Manager (Chunk D):**
- `run-manager --interval N` runs forever with Ctrl-C graceful stop
- Circuit breaker: `--max-failures 5` (configurable) with exponential backoff
- Exits code 1 if too many consecutive failures
- Production-ready for daemon deployment

**Trailing Stop Implementation:**
- R-multiple ratchet: at +1R move to breakeven, at +1.5R move to +0.5R
- Chandelier ATR: `highest_high - (1.5 * ATR)`
- Wilder's smoothing ATR(14) from `trading_bot/data/indicators.py`
- Persists `initial_risk` and `highest_high` on each `Position`
- Only tightens, never loosens

**End-of-Day Exit:**
- Configurable: `close_hour`, `close_minute`, `eod_minutes_before_close`, `eod_enabled`
- Runs first in priority order — no other exits evaluated during EOD window
- Logs as `FILLED reason=eod` to decision log

### V2 Configuration

Add to `config.yaml`:

```yaml
app:
  timezone: "America/New_York"
  
market_data:
  max_data_age_hours: 72  # Skip actions if data older than this

session:
  close_hour: 16
  close_minute: 0
  eod_minutes_before_close: 5  # Exit at 15:55 ET
  eod_enabled: true

paper:
  fee_per_order: 1.0  # Dollars per fill
  slippage_bps: 0     # Basis points of slippage
```

### V2 Safety Features

- Stale data freeze: Won't trade on old data
- Lock handling: Retries on SQLite contention
- Circuit breaker: Daemon exits if consistently failing
- Deterministic exits: Clear priority order, no ambiguity
- Audit trail: Every decision logged to `logs/decision-log.jsonl`

### V2 Test Coverage

161 tests covering:
- Latency detection and staleness handling
- Slippage and fee application
- SQLite lock retry logic
- Circuit breaker behavior
- All four exit types (EOD, stop, target, trail)
- Trailing stop ratcheting logic
- run-manager loop and interrupt handling

## V2 Keep In Mind

*The following section is historical context for the V2 design. V2 is now complete.*

Close trade lifecycle loop:

- ~~Add manage-positions loop for open trades.~~ ✅
- ~~Single-shot `manage-positions` exists and reports open positions.~~ ✅
- ~~Single-shot `manage-positions` now executes hard stop and target exits through the paper broker.~~ ✅
- ~~Each manager run saves updated portfolio state and refreshes `state/portfolio_summary.json`.~~ ✅
- ~~Single-shot `manage-positions` now ratchets trailing stop up only after stop and target checks pass.~~ ✅
- ~~Trail stops persist across runs on each `Position` (`stop_loss`, `highest_high`, `initial_risk`).~~ ✅
- ~~Single-shot `manage-positions` now liquidates open positions at end-of-day (default 15:55 ET on weekdays).~~ ✅
- ~~Position entry time persisted on each `Position` as `entry_at` for audit and future lifecycle rules.~~ ✅
- ~~EOD exit is configurable via the `session:` block (`close_hour`, `close_minute`, `eod_minutes_before_close`, `eod_enabled`).~~ ✅
- ~~Add latency gate to freeze manager on stale data.~~ ✅
- ~~Handle SQLite locks cleanly if scanner and manager run near same time.~~ ✅
- ~~Add strict latency gate between signal data timestamp and local clock.~~ ✅
- ~~Simulate hostile fills with slippage.~~ ✅
- ~~Deduct fees from portfolio state.~~ ✅
- ~~Add continuous `run-manager --interval` with circuit breaker.~~ ✅
- Add macro/breadth master gate. *(Future: V2.1 or V3)*
- Add ATR-based sizing. *(Future)*
- Add push notifications for `GREEN` signals and fills. *(Future)*

Exit order remains: **EOD → Stop → Target → Trail**

## V3 And V4 Roadmap

Robinhood agentic integration fits later, not in V1 or V2.

Why:

- Live execution changes the risk profile of the app.
- Robinhood Agentic Trading uses a dedicated agentic account and Trading MCP flow.
- Robinhood exposes useful review, account, portfolio, market data, and order tools, so we can stage this safely.

V3 goal: shadow integration, no live order placement.

V3 checklist:

- Add broker abstraction only where needed for one external broker path.
- Add Robinhood connector config with credentials stored outside git.
- Add read-only account sync for accounts, buying power, positions, and recent orders.
- Add market data parity checks between local feed and broker-visible quotes.
- Add symbol tradability check before any live-intent workflow.
- Add order preview path that mirrors broker-side review before submit.
- Add shadow mode that logs "would place order" without placing it.
- Add explicit kill switch that blocks any accidental submit path.
- Add audit log rows for broker sync, preview, and shadow decisions.
- Add operator command(s) for account sync, broker health, and preview checks.
- Keep user approval/manual review as the default operating mode.

V4 goal: guarded live execution through broker review plus submit.

V4 checklist:

- Support broker-side order review before every live equity order.
- Support live equity order placement only after review passes.
- Support cancel-open-order flow.
- Require manual approval by default before submit.
- Add optional supervised auto-submit mode only after manual mode is stable.
- Reuse daily loss limit, daily order limit, stale-data gate, and duplicate-position blocks for live mode.
- Add live-only max notional and max position caps.
- Add emergency stop command that disables further submits immediately.
- Add order-status reconciliation so local state matches broker state after fills, partial fills, rejects, or cancels.
- Add startup safety check that refuses live mode if account sync is stale.
- Keep options out of scope until equities path is proven stable.

Suggested sequence:

1. Finish V2 position management and paper realism.
2. Build autonomous scouting for a small-cap-first universe.
3. Add Discord or Telegram alerts on top of scouting and fills.
4. Build V3 read-only broker sync and shadow mode.
5. Run shadow mode for a while and compare decisions against paper flow.
6. Add V4 manual-approval live execution.
7. Only then consider supervised automation.

## Autonomous Scouting Track

Target behavior:

- The app should scout markets on its own.
- The first focus should be U.S. small-cap names, not broad mega-cap-only scanning.
- The app should suggest candidates, not blindly auto-buy them.

Small-cap-first V3 scouting checklist:

- Add a maintained universe file under local state for tradable symbols.
- Start with U.S. listed common stocks and exclude ETFs, funds, warrants, and OTC names in the first slice.
- Add a small-cap segment filter using market-cap bounds.
- Add liquidity filter using average dollar volume so suggestions stay tradable.
- Add price floor to avoid ultra-low-priced names in the first slice.
- Refresh the universe on a schedule instead of hardcoding symbol lists.
- Add a command to rebuild the universe locally.
- Add a command to scan the saved universe without passing `--symbols`.
- Rank candidates by signal quality, freshness, relative volume, and risk/reward.
- Save the top candidates into the existing scan snapshot for dashboard and alerts.
- Keep the first version long-only and U.S.-only.

Suggested first small-cap profile:

- Market cap roughly `$50M` to `$2B`.
- Price above `$2`.
- Minimum average dollar volume threshold so fills are realistic.
- Exclude OTC in the first version.

Alerts checklist:

- Add outbound webhook notifier with no extra service.
- Start with Discord webhook support first because it is dead simple.
- Add Telegram bot support right after Discord.
- Send alerts for new `GREEN` candidates from autonomous scans.
- Send alerts for paper fills, stop exits, target exits, and risk-limit blocks.
- Add a daily market-summary alert with top candidates and portfolio state.
- Add a test-alert command so operators can validate webhooks quickly.
- Keep alerts one-way at first; no chat commands or bot control surface yet.
