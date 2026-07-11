# Getting Started with Autonomous Trading Agent

A complete newbie guide to setting up and running the paper trading bot.

> **Already set up? See [DAILY_STARTUP.md](DAILY_STARTUP.md) for daily operations.**

## Prerequisites

- **macOS/Linux** (Windows works with WSL)
- **Python 3.11+**
- **Git and Stow**

---

## Step 1: Clone and Setup

```bash
# Clone the repository
git clone <your-repo-url>
cd AutonomousTradingAgent

# Create virtual environment
python3 -m venv .venv

# Activate it
source .venv/bin/activate  # macOS/Linux
# OR
.venv\Scripts\activate  # Windows

# Install the package
.venv/bin/python -m pip install -e .[dev]
```

---

## Step 2: Verify Installation

```bash
# Check everything is working
./tradebot-local doctor

# Run tests to confirm
.venv/bin/python -m pytest -q
```

Expected output: hundreds of passing tests. The exact count changes as features are added.

---

## Step 3: Configuration

### Option A: Basic Setup (Paper Trading Only)

```bash
# Copy example environment file
cp .env.example .env

# Edit .env with your settings
nano .env
```

**Critical `.env` settings:**
```bash
CONFIG_PATH=burn-in-config.yaml   # Points to burn-in config
APCA_API_KEY_ID=your_alpaca_key   # For Alpaca data provider
APCA_API_SECRET_KEY=your_secret   # For Alpaca data provider
```

### Option B: With Robinhood Integration (Optional)

```bash
# Robinhood is MCP-only (no direct auth, no live orders)
# Uses operator-synced JSON snapshots + local intent logs
# Enable in burn-in-config.yaml:
# robinhood:
#   enabled: true
#   mode: shadow
```

### Option C: Webhook Alerts (Optional)

```bash
# Add to .env for Discord alerts:
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR/WEBHOOK
```

---

## Step 4: First Commands

### Check System Health
```bash
./tradebot-local doctor
```
Expected: `doctor live_trading=false provider=alpaca provider_auth=ok`

### Scan for Trading Signals
```bash
# Scan single symbol with details
./tradebot-local scan --symbols AAPL --why --summary

# Scan multiple symbols
./tradebot-local scan --symbols SPY,AAPL,MSFT,TSLA --why
```

### Run Paper Trade
```bash
# Execute paper trades on GREEN signals
./tradebot-local paper-trade --symbols AAPL,SPY

# Dry run (preview only)
./tradebot-local paper-trade --symbols AAPL --dry-run
```

### Check Portfolio
```bash
./tradebot-local portfolio
```

### Manage Positions (Stops & Targets)
```bash
./tradebot-local manage-positions
```

### Live Dashboard

Two dashboards are available — pick one:

**A. Rich UI (recommended for desk monitoring)** — Bloomberg-style interface with
real-time SSE updates, sparkline, kill-switch lever, and bento layout:

```bash
./scripts/start-dashboard.sh
# → open http://127.0.0.1:8080
```

See **[`ui/dashboard/README.md`](ui/dashboard/README.md)** for the full guide.

**B. CLI snapshot dashboard** — simpler, built into the CLI:

```bash
./tradebot-local serve
# → open http://127.0.0.1:8000
```

Both bind to localhost only (127.0.0.1) and never expose authentication.

---

## Step 5: Continuous Trading (Burn-In Mode)

> **For daily operations, see [DAILY_STARTUP.md](DAILY_STARTUP.md).**

### Automated Paper Trading

```bash
./scripts/auto-burn-in.sh
```

This will:
- Discover new candidates daily at 9:30 AM ET
- Scan 150 symbols every 60 seconds during market hours
- Auto-trade GREEN signals (V3 strategy: regime + confluence scoring)
- Manage positions (stops, targets, EOD exits, counter-thesis exits)
- Log all decisions to `logs/burn_in/decision-log.jsonl`

**Config:** `burn-in-config.yaml` (auto-loaded via `CONFIG_PATH` in `.env`)
**Data provider:** Alpaca (with yfinance fallback)
**Database:** `state/burn_in.db` (separate from default `state/trading_bot.db`)

### Monitor Burn-In

```bash
# Rich UI dashboard (recommended)
./scripts/start-dashboard.sh --config burn-in-config.yaml
# → open http://127.0.0.1:8080
# See ui/dashboard/README.md for full guide

# Simpler CLI dashboard (alternative)
./tradebot-local serve
# → open http://127.0.0.1:8000

# Live decision log
tail -f logs/burn_in/decision-log.jsonl

# Daily status
./scripts/burn-in-monitor.sh

# Weekly review (run Fridays)
./scripts/burn-in-weekly-review.sh
```

### Stop Burn-In
Press `Ctrl+C` to stop safely.

---

## Step 6: View Performance

### Live Dashboard
```bash
# Serve live dashboard (auto-refreshes every 5s)
./tradebot-local serve
# → open http://127.0.0.1:8000
```

### Static Dashboard (for snapshots)
```bash
# Generate static HTML
./tradebot-local dashboard --output state/dashboard.html
open state/dashboard.html  # macOS
```

### Performance Metrics
```bash
# View performance summary
./tradebot-local performance --days 30

# Daily breakdown
./tradebot-local performance --days 7 --daily
```

### Health & Alerts
```bash
./tradebot-local health    # System health
./tradebot-local alerts    # Active alerts
```

---

## Step 7: Safety Controls

### Kill Switch (Emergency Stop)

```bash
# Check status
./tradebot-local kill-switch --status

# HALT all trading
./tradebot-local kill-switch --halt --reason "Emergency stop"

# Resume trading
./tradebot-local kill-switch --resume
```

---

## Common Workflows

### Daily Trading Session

```bash
# 1. Pre-flight (30s)
./tradebot-local doctor
./tradebot-local kill-switch --status
./tradebot-local portfolio

# 2. Start automated trading (leave running)
./scripts/auto-burn-in.sh

# 3. Monitor in separate terminal
./tradebot-local serve  # Live dashboard at localhost:8000
```

### Backtesting

```bash
# Run backtest on date range
./tradebot-local backtest --symbols AAPL --start 2025-01-01 --end 2025-06-01
```

---

## Troubleshooting

### Tests Failing
```bash
# Reinstall dependencies
.venv/bin/python -m pip install --force-reinstall -e .[dev]

# Check NumPy version (should be <2)
.venv/bin/python -c "import numpy; print(numpy.__version__)"

# Fix NumPy if needed
.venv/bin/python -m pip install --force-reinstall "numpy<2" "pandas>=2.2"
```

### Import Errors
```bash
# Always use the local wrapper
./tradebot-local --help

# NOT just 'tradebot' (might use wrong environment)
```

### Stale Data Errors
```bash
# Check burn-in-config.yaml
market_data:
  max_data_age_hours: 72   # Daily data freshness
  max_data_age_minutes: 30  # Intraday data freshness
  validate_data: true       # V2.5 fail-fast on bad data
```

### Permission Errors
```bash
# Fix database permissions
chmod 600 state/burn_in.db
chmod 700 state/
```

---

## Project Structure

```
AutonomousTradingAgent/
├── trading_bot/           # Main code
│   ├── cli/app.py        # CLI commands
│   ├── data/             # Market data & indicators
│   ├── strategy/         # Signal generation
│   ├── risk/             # Position sizing
│   ├── execution/        # Paper broker
│   ├── portfolio/        # Ledger & P&L
│   ├── monitoring/       # Health & alerts
│   ├── runtime/          # Orchestrator
│   └── safety/           # Kill switch
├── tests/                # All tests
├── scripts/              # Automation scripts
├── docs/                 # Documentation
├── config.yaml          # Main config
├── tradebot-local       # CLI wrapper ⭐
└── GETTING_STARTED.md   # This file
```

---

## Key Commands Cheat Sheet

| Command | Purpose |
|---------|---------|
| `./tradebot-local doctor` | Check system health |
| `./tradebot-local serve` | Live dashboard (localhost:8000) |
| `./tradebot-local scan --symbols SPY --why` | Scan for signals |
| `./tradebot-local paper-trade --symbols SPY` | Execute paper trades |
| `./tradebot-local manage-positions` | Check stops & targets |
| `./tradebot-local portfolio` | View portfolio |
| `./tradebot-local performance --daily` | Performance metrics |
| `./tradebot-local health` | System health |
| `./tradebot-local kill-switch --status` | Check kill switch |
| `./scripts/auto-burn-in.sh` | Automated trading |

---

## Safety Reminders

⚠️ **This is paper trading only** - No real money at risk
⚠️ **Live trading is disabled** in code (`live_trading_enabled = False`)
⚠️ **Position size capped at 20%** per ticker
⚠️ **3% portfolio heat limit** blocks new trades on unrealized losses
⚠️ **Kill switch** stops all trading instantly
⚠️ **Dashboard binds to localhost only** (127.0.0.1, not 0.0.0.0)
⚠️ **Never commit** credentials to git (use .env)

---

## Next Steps

1. ✅ Run burn-in for 2-4 weeks to validate strategy
2. ✅ Review performance metrics daily
3. ✅ Use `./tradebot-local serve` for live monitoring
4. ⏳ V3: Robinhood integration (shadow mode)

---

## Getting Help

- **Daily operations**: [DAILY_STARTUP.md](DAILY_STARTUP.md)
- **Quick reference**: [QUICK_REFERENCE.md](QUICK_REFERENCE.md)
- **Docs**: Check `docs/` folder
- **Tests**: Run `.venv/bin/python -m pytest -v tests/test_<feature>.py`
- **Logs**: Check `logs/` folder
- **Config**: Edit `burn-in-config.yaml` for burn-in settings

---

**Remember:** This is a paper trading system for testing strategies. Always validate thoroughly before considering live trading.
