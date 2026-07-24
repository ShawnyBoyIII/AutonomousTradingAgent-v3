# Quick Reference Card

## Setup (One-Time)

```bash
python3 -m venv .venv
source .venv/bin/activate
.venv/bin/python -m pip install -e .[dev]
cp .env.example .env  # then edit with Alpaca keys
```

---

## Daily Commands

> **For full daily instructions, see [DAILY_STARTUP.md](DAILY_STARTUP.md)**

### Morning
```bash
./tradebot-local doctor                # Health check
./tradebot-local kill-switch --status  # Safety check
./scripts/auto-burn-in.sh              # Start trading (leave running)
```

### Monitoring (separate terminals)
```bash
./tradebot-local serve                 # Live dashboard → http://127.0.0.1:8000
./scripts/start-dashboard.sh           # Same app, standalone launcher → :8080
tail -f logs/burn_in/decision-log.jsonl # Live decision feed
./tradebot-local portfolio             # Positions + P&L
./tradebot-local performance --daily   # Daily metrics
```

### End of Day
```bash
# Ctrl-C the burn-in script
./tradebot-local performance --daily
./tradebot-local portfolio
```

---

## Emergency
```bash
./tradebot-local kill-switch --halt --reason "STOP"  # Halt all trading
./tradebot-local kill-switch --resume                 # Resume
```

---

## Key Commands

| Command | What It Does |
|---------|-------------|
| `./scripts/auto-burn-in.sh` | Automated trading loop |
| `./tradebot-local serve` | Live dashboard (localhost:8000) |
| `./scripts/start-dashboard.sh` | Same dashboard app (localhost:8080) |
| `doctor` | System health check |
| `scan --symbols SPY,AAPL --why` | Scan for signals |
| `paper-trade --symbols AAPL` | Execute paper trades |
| `manage-positions` | Check stops/targets/EOD |
| `backtest --symbols AAPL --start YYYY-MM-DD --end YYYY-MM-DD` | Replay one strategy |
| `portfolio` | View holdings + P&L |
| `performance --daily` | Performance metrics |
| `health` | System status |
| `alerts` | Active warnings |
| `kill-switch --status` | Check kill switch |
| `robinhood-status` | Show MCP snapshot state |
| `serve` | Live web dashboard |

---

## Test Commands
```bash
.venv/bin/python -m pytest -q                         # All tests
.venv/bin/python -m pytest tests/test_indicators.py -v # Specific file
```

---

## Config Files
- `burn-in-config.yaml` — Burn-in config (Alpaca provider, V3 strategy, risk limits)
- `config.yaml` — Default config (fallback)
- `.env` — Secrets + `CONFIG_PATH=burn-in-config.yaml` (don't commit!)
- `state/universe.txt` — runtime burn-in universe
- `state/burn_in.db` — Paper trading ledger
- `logs/burn_in/` — Decision logs + strategy results

---

## Data Provider
- **Alpaca** (default for burn-in) — real-time data, requires API keys in `.env`
- **yfinance** (fallback) — free, no auth, used automatically when Alpaca fails
- Switch in `burn-in-config.yaml`: `market_data.provider: alpaca` or `yfinance`

```bash
# .env must have (for Alpaca):
APCA_API_KEY_ID=your_key
APCA_API_SECRET_KEY=your_secret
```

---

## Config Priority
1. `--config-path` flag (highest)
2. `CONFIG_PATH` env var (set in `.env`)
3. `config.yaml` (default)

With `CONFIG_PATH=burn-in-config.yaml` in `.env`, bare commands use the burn-in config automatically.

---

## Safety Limits
- Paper trading only (hardcoded `live_trading_enabled = False`)
- 20% max position per ticker
- 3% portfolio heat limit
- Kill switch at all entry points
- Dashboard binds to localhost only

---
**Always use `./tradebot-local` not bare `tradebot`**
