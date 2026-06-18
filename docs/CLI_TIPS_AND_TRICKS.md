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

### 6. Run Position Manager

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
- Logs `FILLED reason=eod`, `FILLED reason=stop`, `FILLED reason=target`, and `TRAIL` events to `logs/decision-log.jsonl`.
- Updates local portfolio state after exits and trails.

### 7. View Report

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

### 8. Run Backtest

```bash
sh ./tradebot-local backtest --symbols AAPL,MSFT,SPY,NVDA,QQQ --start 2026-05-01 --end 2026-06-17
```

What it does:

- Replays historical data.
- Prints trades, wins, win rate, and net PnL.

### 9. Build Local Dashboard

```bash
sh ./tradebot-local dashboard --output state/dashboard.html
```

What it does:

- Builds a static HTML dashboard from local JSON snapshots.
- Useful when you want a quick visual without building a real UI.

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
sh ./tradebot-local backtest --symbols SPY,QQQ --start 2026-05-01 --end 2026-06-17
sh ./tradebot-local dashboard --output state/dashboard.html
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

### `--config-path`

Use a custom config file:

```bash
sh ./tradebot-local --config-path config.yaml scan --symbols SPY
```

## Runtime Files To Know

Generated under `state/`:

- `trading_bot.db`
- `scan_results.json`
- `portfolio_summary.json`
- `dashboard_summary.json`
- `backtest_summary.json`
- `dashboard.html`

Generated under `logs/`:

- `decision-log.jsonl`

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

## Best Habits

- Run `doctor` before blaming the strategy.
- Use `scan --why` more than plain `scan`.
- Use `paper-trade --dry-run` before real paper fills.
- Check `portfolio` before and after `manage-positions`.
- Use `report` for totals, not memory.
- Rebuild `dashboard.html` when you want a visual snapshot.
