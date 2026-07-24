# CLI Tips And Tricks

This app is CLI-first for now.

Use this file as the no-UI operator cheat sheet.

## Golden Rule

Always run the app with:

```bash
cd /Users/shawndlima/Documents/AutonomousTradingAgent
sh ./tradebot-local --help
```

Do not rely on a global `tradebot` command unless you know it points at this repo's `.venv`.

## Core Commands

### 1. Health Check

Use this first if something feels off.

```bash
sh ./tradebot-local doctor
```

What it does:

- Checks local app readiness.
- Does not fetch market data.

### 2. Scan Symbols

Basic scan:

```bash
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY,NVDA,QQQ
```

Detailed scan:

```bash
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY,NVDA,QQQ --why
```

Detailed scan with totals:

```bash
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY,NVDA,QQQ --why --summary
```

What it does:

- Looks for long candidates.
- Returns `APPROVED`, `NO_SIGNAL`, `REJECTED`, or `ERROR`.
- `--why` shows the gate values and reasons.
- `--summary` prints one totals line at the end.

### 3. Preview A Paper Trade

Use dry run before writing anything.

```bash
sh ./tradebot-local paper-trade --symbols SPY --dry-run
```

What it does:

- Simulates the paper-trade decision.
- Does not write fills.
- Does not update portfolio state.

### 4. Run Paper Trade

```bash
sh ./tradebot-local paper-trade --symbols SPY
```

What it does:

- Attempts a paper fill only for valid fresh `GREEN` signals.
- Writes orders and portfolio state if filled.

### 5. View Portfolio

```bash
sh ./tradebot-local portfolio
```

What it does:

- Shows cash, equity, realized PnL, unrealized PnL, exposure, and position count.
- Shows each open position with qty, average cost, last price, market value, unrealized PnL, and allocation.

### 6. Run Position Manager (Single-Shot)

```bash
sh ./tradebot-local manage-positions
```

What it does:

- Checks all open positions.
- Liquidates everything at end-of-day (default 15:55 ET weekdays) to avoid overnight gap.
- Fills stop-loss exits.
- Fills profit-target exits.
- Ratchets trailing stop up only when price advances (R-multiple and chandelier ATR).
- Persists `stop_loss`, `highest_high`, `initial_risk`, and `entry_at` per position.
- **Skips all actions if market data is stale** (older than `market_data.max_data_age_hours`).
- Logs `FILLED reason=eod`, `FILLED reason=stop`, `FILLED reason=target`, and `TRAIL` events to `logs/decision-log.jsonl`.
- Updates local portfolio state after exits and trails.
- Prints `positions=N actions=A skipped=S` summary line.

### 7. Run Position Manager (Continuous Daemon)

```bash
# Run every 60 seconds until Ctrl-C
sh ./tradebot-local run-manager --interval 60

# Tight loop (useful for testing, not recommended for production)
sh ./tradebot-local run-manager --interval 0

# Fail fast on errors (circuit breaker after 3 failures)
sh ./tradebot-local run-manager --interval 60 --max-failures 3
```

What it does:

- Runs `manage-positions` logic in a continuous loop.
- Waits `--interval` seconds between iterations (0 = no wait).
- Handles Ctrl-C gracefully: prints "run-manager stopped" and exits cleanly.
- Circuit breaker: after `--max-failures` consecutive errors (default 5), exits with code 1.
- Exponential backoff on failures: 1s, 2s, 4s, 8s... up to 30s between retries.
- **Production use:** Run in a systemd service, Docker container, or tmux/screen session.
- **Logs:** Check `logs/decision-log.jsonl` for iteration events and errors.

### 8. View Report

```bash
sh ./tradebot-local report
```

Export JSON summary:

```bash
sh ./tradebot-local report --json-path state/manual-report.json
```

Export CSV order history:

```bash
sh ./tradebot-local report --csv-path state/orders.csv
```

What it does:

- Prints performance totals.
- Can export summary JSON.
- Can export order history CSV.

### 9. Run Backtest

Standard backtest:

```bash
sh ./tradebot-local backtest --symbols AAPL,MSFT,SPY,NVDA,QQQ --start 2026-05-01 --end 2026-06-17
```

Walk-forward analysis (regime-stability check):

```bash
# Split into 6 windows, see per-period + aggregated results
sh ./tradebot-local backtest --symbols AAPL --start 2026-01-01 --end 2026-06-01 --walk-forward --windows 6
```

What it does:

- Replays historical data.
- Prints trades, wins, win rate, and net PnL.
- `--walk-forward` splits the date range into N sequential windows and runs an independent backtest on each. Consistent results across all windows = robust strategy. High variance = overfit or regime-dependent.

### 10. Strategy Health

```bash
sh ./tradebot-local strategy-health
sh ./tradebot-local strategy-health --window 30
```

What it does:

- Shows per-strategy performance over a rolling window of exits.
- Displays total exits, recent wins/losses, win rate, net PnL, and allocation status.
- Allocation labels: `full` (1.0x), `half` (0.5x), or `skip` (0.0x).
- Allocation logic:
  - `< 20 exits`: full allocation (insufficient data to judge)
  - `≥ 50% win rate`: full allocation
  - `40–50% win rate`: half allocation
  - `< 40% win rate`: strategy skipped
- Affects which signals get through in `paper-trade` — skipped strategies are rejected before the position sizer runs.

### 11. Launch The Live Dashboard

```bash
sh ./tradebot-local serve
```

What it does:

- Launches the canonical `ui/dashboard/main.py` FastAPI + SSE + Jinja app.
- Defaults to `app.dashboard_port` (8000 in `config.yaml`).
- `./scripts/start-dashboard.sh` launches the same app on port 8080 by default.

## Most Useful Workflows

### Morning Check

```bash
sh ./tradebot-local doctor
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY,NVDA,QQQ --why --summary
sh ./tradebot-local portfolio
```

### Safe Trade Workflow

```bash
sh ./tradebot-local scan --symbols SPY,QQQ --why --summary
sh ./tradebot-local paper-trade --symbols SPY --dry-run
sh ./tradebot-local paper-trade --symbols SPY
sh ./tradebot-local portfolio
sh ./tradebot-local report
```

### Open Position Check

```bash
sh ./tradebot-local portfolio
sh ./tradebot-local manage-positions
sh ./tradebot-local portfolio
```

### End Of Session Review

```bash
sh ./tradebot-local report
sh ./tradebot-local strategy-health
sh ./tradebot-local backtest --symbols SPY,QQQ --start 2026-05-01 --end 2026-06-17
sh ./tradebot-local serve
```

### Strategy Health Check

```bash
# Quick per-strategy performance view
sh ./tradebot-local strategy-health

# Walk-forward to verify strategy robustness across regimes
sh ./tradebot-local backtest --symbols SPY,AAPL --start 2025-06-01 --end 2026-06-01 --walk-forward --windows 6

# Compare V2.5 vs V3 signals in backtest (requires V3 config)
sh ./tradebot-local backtest --symbols SPY --start 2026-01-01 --end 2026-06-01
```

### Production Daemon Workflow

Start the manager as a background daemon:

```bash
# In a tmux/screen session or systemd service
sh ./tradebot-local run-manager --interval 60 --max-failures 5
```

Monitor it:

```bash
# Check if it's running
ps aux | grep run-manager

# View recent decisions
tail -f logs/decision-log.jsonl

# Check circuit breaker hasn't triggered
tail -5 logs/decision-log.jsonl | grep -E "(stopped|circuit)"
```

Stop it gracefully:

```bash
# Send Ctrl-C (SIGINT) — not SIGKILL
# In tmux: Ctrl-C
# From outside: kill -INT <pid>
```

## Useful Flags

### `--symbols`

Use comma-separated tickers:

```bash
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY
```

### `--why`

Adds reasoning and gate values:

```bash
sh ./tradebot-local scan --symbols SPY --why
```

### `--summary`

Adds one totals line:

```bash
sh ./tradebot-local scan --symbols SPY,QQQ --summary
```

### `--dry-run`

Preview only:

```bash
sh ./tradebot-local paper-trade --symbols SPY --dry-run
```

### `--start` and `--end`

Used by backtest:

```bash
sh ./tradebot-local backtest --symbols SPY --start 2026-06-01 --end 2026-06-17
```

### `--walk-forward` and `--windows`

Walk-forward analysis splits the date range into sequential windows. Use with `backtest`:

```bash
# 6 windows across the full range
sh ./tradebot-local backtest --symbols AAPL --start 2026-01-01 --end 2026-06-01 --walk-forward --windows 6
```

### `--config-path`

Use a custom config file:

```bash
sh ./tradebot-local --config-path config.yaml scan --symbols SPY
```

## V2 Configuration

Add to your `config.yaml` for full V2 features:

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

## Runtime Files To Know

Generated under `state/`:

- `trading_bot.db`
- `scan_results.json`
- `portfolio_summary.json`
- `dashboard_summary.json`
- `backtest_summary.json`

Generated under `logs/`:

- `decision-log.jsonl` — every decision (scan, fill, exit)
- `strategy_results.jsonl` — per-strategy entry/exit events (used by `strategy-health`)

## Fast Troubleshooting

### If `tradebot` points to the wrong install

Check:

```bash
which tradebot
```

If it points somewhere outside this repo, use:

```bash
sh ./tradebot-local --help
```

### If you want the direct Python path

```bash
.venv/bin/python -m trading_bot.main scan --symbols AAPL
```

### If nothing is filling

Common reasons:

- No signal.
- Signal is stale.
- Signal is `YELLOW`.
- Daily order limit was hit.
- Daily loss limit was hit.
- Duplicate ticker is already open.
- Insufficient cash.

Start with:

```bash
sh ./tradebot-local scan --symbols AAPL,MSFT,SPY,NVDA,QQQ --why --summary
sh ./tradebot-local report
sh ./tradebot-local portfolio
```

### If manage-positions shows "skipped=N"

This means market data is stale (older than `market_data.max_data_age_hours`).

Check:

```bash
# View last data timestamp
sh ./tradebot-local portfolio
# Look for data age warnings in decision-log.jsonl
head -20 logs/decision-log.jsonl
```

Fix: Wait for fresh data or increase `max_data_age_hours` (not recommended for production).

### If run-manager exits with code 1

Circuit breaker opened after too many consecutive failures.

Check:

```bash
# View recent errors
tail -50 logs/decision-log.jsonl | grep error
```

Common causes:

- SQLite database locked (another process is writing).
- Network failures fetching market data.
- Corrupted portfolio state.

Fix: Check `doctor` output, ensure no other processes are using the database, restart run-manager.

### If trailing stops aren't updating

Check:

```bash
# View position details
sh ./tradebot-local portfolio
# Look for TRAIL events in decision log
grep TRAIL logs/decision-log.jsonl
```

Common reasons:

- Price hasn't advanced enough to trigger R-multiple ratchet.
- ATR data missing (need daily bars with high/low).
- Position already exited via stop/target/EOD.

## Best Habits

- Run `doctor` before blaming the strategy.
- Use `scan --why` more than plain `scan`.
- Use `paper-trade --dry-run` before real paper fills.
- Check `portfolio` before and after `manage-positions`.
- **Use `run-manager` in production** instead of cron jobs calling `manage-positions`.
- Monitor `logs/decision-log.jsonl` for skipped iterations (stale data).
- Set `--max-failures` low in production (3-5) to fail fast on systemic issues.
- Check circuit breaker exits immediately — they indicate real problems.
- **Run `strategy-health` weekly** to catch degrading strategies before they drain PnL.
- **Review strategies with `half` or `skip` allocation** — they may need re-tuning or retirement.
- **Use `backtest --walk-forward` to validate strategy robustness** before deploying new signals.
- Use `report` for totals, not memory.
- Use `serve` or `scripts/start-dashboard.sh` for the live dashboard; both launch the same app.
- Review `docs/adr/001-exit-priority-order.md` to understand exit precedence.
