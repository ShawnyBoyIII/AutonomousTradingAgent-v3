# Daily Startup Guide

The "open this every morning" reference. For one-time setup, see [GETTING_STARTED.md](GETTING_STARTED.md).

---

## Quick Start (2 minutes)

```bash
# Terminal 1 — Pre-flight (30s)
./tradebot-local doctor
./tradebot-local kill-switch --status
./tradebot-local portfolio

# Terminal 1 — Start trading (leave running)
./scripts/auto-burn-in.sh

# Terminal 2 — Live dashboard (optional)
./tradebot-local serve
# → open http://127.0.0.1:8000
```

---

## Pre-Flight Checks (30 seconds)

Run these before starting the burn-in:

```bash
./tradebot-local doctor               # System health + provider check
./tradebot-local kill-switch --status  # Ensure trading is enabled (exit 0 = active)
./tradebot-local portfolio             # Current positions + P&L
```

**Expected:**
- `doctor` → `live_trading=false provider=alpaca provider_auth=ok`
- `kill-switch --status` → `🟢 KILL SWITCH: Trading active` (exit 0)
- `portfolio` → your equity, cash, open positions

If kill switch is active, resume with:
```bash
./tradebot-local kill-switch --resume
```

---

## Start Trading

```bash
./scripts/auto-burn-in.sh
```

**What it does:**
- Runs `discover` daily at 9:30 AM ET to refresh the universe (`state/universe.txt`)
- Scans all 150 symbols every 60 seconds during market hours (9:30 AM – 4:00 PM ET, weekdays)
- Auto-trades GREEN signals via V3 strategy (regime + confluence scoring)
- Manages positions: stop-loss, profit target, end-of-day exit, counter-thesis exit
- Skips weekends and after-hours automatically
- Logs every decision to `logs/burn_in/decision-log.jsonl`

**Config file:** `burn-in-config.yaml` (auto-loaded via `CONFIG_PATH` in `.env`)
**Database:** `state/burn_in.db`
**Symbols:** `state/universe.txt` plus optional `state/watchlist.txt`

**Stop:** Press `Ctrl-C`

---

## Monitoring (separate terminals)

### Live Dashboard (recommended)

```bash
./tradebot-local serve
```
Open `http://127.0.0.1:8000` in a browser. Auto-refreshes every 5 seconds.

Shows: equity, cash, exposure, P&L, kill-switch banner, open positions, scan candidates, live decision feed, recent trade exits.

**Routes:**
- `/` — HTML dashboard (auto-refresh)
- `/api/state` — JSON snapshot
- `/healthz` — `"ok"`

Binds to localhost only (127.0.0.1) for security.

### Live Decision Log

```bash
tail -f logs/burn_in/decision-log.jsonl
```

Each line is a JSON object: `{"command": "scan", "ticker": "AAPL", "status": "APPROVED", ...}`

### Status Commands (anytime)

```bash
./tradebot-local portfolio            # Positions + P&L
./tradebot-local health               # System health + alerts
./tradebot-local alerts               # Active alerts
./tradebot-local performance --daily  # Daily P&L breakdown
./tradebot-local manage-positions     # Check stops/targets/EOD
```

### Burn-In Monitor Scripts

```bash
./scripts/burn-in-monitor.sh          # Daily status summary
./scripts/burn-in-weekly-review.sh    # Weekly analysis (run Fridays)
```

---

## End of Day

```bash
# 1. Stop the burn-in script
# (in the burn-in terminal) press Ctrl-C

# 2. Review performance
./tradebot-local performance --daily

# 3. Check final portfolio state
./tradebot-local portfolio

# 4. Fridays: weekly review
./scripts/burn-in-weekly-review.sh
```

---

## Emergency Controls

```bash
# HALT all trading immediately
./tradebot-local kill-switch --halt --reason "Emergency stop"

# Resume trading
./tradebot-local kill-switch --resume

# Check status
./tradebot-local kill-switch --status
```

When kill switch is active, the burn-in script will skip all trade cycles and log `"KILL_SWITCH"` until resumed.

---

## Common Issues

### `state/universe.txt` is empty (scanning 0–1 symbols)

The `discover --export` step updates this file daily. If discovery finds 0 candidates, the existing file is preserved, but if you need to restore defaults:

```bash
# Restore default symbols
mkdir -p state
printf 'SPY\nQQQ\nAAPL\nMSFT\nNVDA\n' > state/universe.txt
```

### `fetch_failed ... falling back to yfinance`

Alpaca API call failed. Check:
1. `.env` has `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` set
2. `burn-in-config.yaml` has `provider: "alpaca"`
3. Network connectivity

The bot falls back to yfinance automatically, so trading continues — but Alpaca is faster and more reliable.

### `stale market data` rejections on paper-trade

This is **expected after market close**. The bot correctly rejects trades on stale prices. During market hours, this shouldn't occur. If it does:
```bash
# Check data freshness settings in burn-in-config.yaml
# market_data.max_data_age_minutes: 30  (intraday)
# market_data.max_data_age_hours: 72    (daily)
```

### `daily order limit` rejections

You hit `risk.max_daily_orders` (default 3). The scanner found more GREEN signals than allowed. To increase throughput:
```bash
# Edit burn-in-config.yaml
# risk.max_daily_orders: 10  # or whatever you want
```

### Portfolio shows all zeros (`cash=10000.00 positions=0`)

You're reading the wrong config/database. Verify:
```bash
echo $CONFIG_PATH  # should be "burn-in-config.yaml"
```
If empty, ensure `.env` has `CONFIG_PATH=burn-in-config.yaml` and restart your shell.

---

## Cheat Sheet

| Command | Purpose |
|---------|---------|
| `./scripts/auto-burn-in.sh` | Start automated trading |
| `./tradebot-local serve` | Live dashboard at localhost:8000 |
| `./tradebot-local doctor` | System health check |
| `./tradebot-local portfolio` | Positions + P&L |
| `./tradebot-local performance --daily` | Daily performance metrics |
| `./tradebot-local health` | System + alert status |
| `./tradebot-local kill-switch --status` | Check kill switch |
| `./tradebot-local kill-switch --halt --reason "X"` | Emergency stop |
| `./tradebot-local kill-switch --resume` | Resume trading |
| `./tradebot-local manage-positions` | Check stops/targets/EOD |
| `./tradebot-local scan --symbols AAPL --why` | Manual scan with details |
| `tail -f logs/burn_in/decision-log.jsonl` | Live decision feed |

---

## Key Files

| File | Purpose |
|------|---------|
| `burn-in-config.yaml` | Burn-in configuration (Alpaca, risk, V3 strategy) |
| `.env` | Credentials + `CONFIG_PATH=burn-in-config.yaml` |
| `state/universe.txt` | Runtime burn-in universe |
| `state/burn_in.db` | Paper trading ledger + positions |
| `logs/burn_in/decision-log.jsonl` | Every scan/trade/exit decision |
| `logs/burn_in/strategy_results.jsonl` | Entry/exit events with P&L |

---

**Safety reminders:**
- Paper trading only (hardcoded `live_trading_enabled: false`)
- 20% max position per ticker
- 3% portfolio heat limit blocks new trades
- Kill switch integrated at all entry points
- Always use `./tradebot-local` (not bare `tradebot`)
