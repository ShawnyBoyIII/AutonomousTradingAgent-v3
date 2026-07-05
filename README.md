# Autonomous Trading Agent

A CLI-first paper-trading system with risk controls, scout/universe building, and an MCP-backed Robinhood boundary for snapshot sync plus operator-reviewed intents.

## Features

- **Paper Trading**: Risk-free simulation with realistic fill prices, slippage, and fees
- **ATR-Based Position Sizing**: Volatility-adjusted position sizes with 20% max allocation per ticker
- **Kill Switch**: Emergency halt/resume for all trading operations
- **Automated Burn-In**: Continuous paper trading with scheduled position management
- **Data Validation**: Real-time OHLC coherence and price sanity checks
- **Portfolio Heat Monitoring**: Blocks new trades when unrealized losses exceed 3%
- **Robinhood MCP Boundary**: Local CLI reads synced snapshots and writes operator-reviewed intents
- **Enhanced Strategies**: Mean reversion signals (Bollinger, VWAP, RSI)
- **Real-Time Dashboard**: Visual charts for trade distribution and performance
- **Webhook Alerts**: Slack/Discord notifications for critical events
- **Live P&L Tracking**: Real-time portfolio monitoring with alerts
- **RL Research Lane**: Train, evaluate, and benchmark RL agents against rule-based paths

## Quick Start

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .[dev]

# Verify installation
./tradebot-local doctor

# Run tests
.venv/bin/python -m pytest -q
```

**Always use `./tradebot-local`** instead of bare `tradebot` to ensure you're using the local virtual environment.

## Common Commands

```bash
# Build and scan a ranked small-cap universe
./tradebot-local build-universe
./tradebot-local scan-universe --summary

# Scan for trade signals (dry run)
./tradebot-local scan --symbols SPY,AAPL --why --summary

# Execute paper trades on GREEN signals
./tradebot-local paper-trade --symbols SPY,AAPL

# Manage positions (check stops, targets, EOD exits)
./tradebot-local manage-positions

# View portfolio and performance
./tradebot-local portfolio
./tradebot-local performance --daily

# Kill switch operations
./tradebot-local kill-switch --status
./tradebot-local kill-switch --halt --reason "Emergency stop"
./tradebot-local kill-switch --resume

# Backtest strategy
./tradebot-local backtest --symbols AAPL --start 2025-01-01 --end 2025-06-01

# RL model coverage and safe scan plan
./tradebot-local rl-model-info
./tradebot-local rl-scan-plan

# RL benchmark against V2.5/V3
./tradebot-local rl-benchmark --symbol AAPL --start 2025-01-01 --end 2025-06-01
./tradebot-local rl-benchmark --symbols AAPL,MSFT --start 2025-01-01 --end 2025-06-01

# Alerts and ops loop
./tradebot-local alert-signals
./tradebot-local alert-exits
./tradebot-local run-ops --cycles 1

# V3: Robinhood MCP snapshot mode
./tradebot-local robinhood-status
./tradebot-local sync-account
./tradebot-local sync-positions
```

## Burn-In Automation

Run continuous paper trading during market hours:

```bash
./scripts/auto-burn-in.sh
```

- Scans every 60 seconds (9:30-16:00 ET, weekdays only)
- Auto-trades GREEN signals
- Manages positions (stops, targets, EOD at 15:55)
- Logs decisions to `logs/burn_in/decision-log.jsonl`

**Monitor burn-in:**
```bash
./scripts/burn-in-monitor.sh         # Daily status
./scripts/burn-in-weekly-review.sh   # Weekly analysis
tail -f logs/burn_in/decision-log.jsonl
```

## Safety

This system is **paper-only by default**. Key safety features:

- `live_trading_enabled` is forced off unless explicit environment safety gates are satisfied
- Position sizes capped at 20% equity per ticker
- Kill switch integrated at all entry points
- Local Robinhood support is MCP/operator-managed rather than direct CLI auth
- Database files use 600 permissions (user-only access)

## Configuration

Edit `config.yaml` or pass `--config-path custom.yaml`:

```yaml
app:
  live_trading_enabled: false  # Always false, enforced in code
  state_db_path: state/trading_bot.db
  universe_path: state/universe.txt
  universe_candidates_path: state/universe_candidates.json
  log_dir: logs

scout:
  screeners: ["aggressive_small_caps", "small_cap_gainers"]
  max_universe_size: 50

risk:
  max_risk_per_trade_pct: 0.01      # 1% risk per trade
  max_daily_orders: 3                # Max 3 orders per day
  max_ticker_allocation_pct: 0.20   # 20% max per ticker
  use_atr_sizing: true              # ATR-based position sizing
  max_portfolio_heat_pct: 0.03      # Halt new trades at 3% unrealized loss

market_data:
  intraday_interval: "5m"
  validate_data: true               # Fail-fast on bad data
```

**Important:** The supported Robinhood flow is MCP/operator-managed snapshot sync. The local CLI does not use direct Robinhood credentials.

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
```

## Troubleshooting

**Tests failing:**
```bash
# Run specific test with verbose output
.venv/bin/python -m pytest tests/test_kill_switch.py::test_kill_switch_status -v

# Check for NumPy 2 issues
.venv/bin/python -m pip install --force-reinstall "numpy<2" "pandas>=2.2" "pyarrow" "numexpr" "bottleneck"
```

**Position sizes too small:**
- Verify `max_ticker_allocation_pct` (default 0.20 = 20%)
- Check `max_portfolio_heat_pct` (blocks at 3% unrealized loss)
- Review ATR multiplier settings

**Stale data errors:**
- Check `max_data_age_hours` (daily) / `max_data_age_minutes` (intraday)
- Ensure `validate_data: true` in config

## Project Structure

```
trading_bot/
├── cli/app.py           # CLI commands (Typer)
├── config/              # Settings and YAML loader
├── data/                # Market data, validation, indicators
├── brokers/             # V3: Robinhood MCP boundary + legacy reference code
├── execution/           # Paper broker, order management
├── portfolio/           # Ledger, P&L tracking
├── risk/                # Position sizing, risk limits
├── strategy/            # Signal generation, scouting, trailing stops
├── safety/              # Kill switch
├── runtime/             # Orchestrator, decision logging
└── monitoring/          # Health checks, alerts
```

## Development

**When fixing bugs:**
1. Reproduce with test (tests are source of truth)
2. Fix implementation
3. Ensure `pytest -q` passes
4. Do not modify test logic

**When adding features:**
1. Add tests first
2. Implement feature
3. Update relevant docs in `docs/`
4. Ensure `pytest -q` passes

## Documentation

**New to the project?** Start here:
- `GETTING_STARTED.md` - Complete newbie guide with all commands
- `QUICK_REFERENCE.md` - One-page cheat sheet
- `ARCHITECTURE.md` - System design and data flow
- `setup.sh` - Automated installation script

**Reference:**
- `AGENTS.md` - Essential context for AI assistants
- `docs/V2_5_PHASE_D_BURN_IN_GUIDE.md` - Burn-in operational guide
- `docs/V3_ROADMAP.md` - Historical V3 notes plus current MCP boundary direction
- `docs/RL_TRADING_GUIDE.md` - Current RL train/eval/benchmark commands
- `docs/SECURITY_HARDENING.md` - Security improvements
- `docs/CLI_TIPS_AND_TRICKS.md` - Advanced CLI usage

## License

MIT
