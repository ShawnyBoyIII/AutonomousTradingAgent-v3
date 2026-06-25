# System Architecture Overview

## Data Flow

```
Market Data (yfinance)
    ↓
Data Validation (OHLC, Price, Volume)
    ↓
Indicators (EMA/SMA/RSI/ATR/MACD/Bollinger/VWAP)
    ↓
Strategy Engine (Trend Following + Mean Reversion)
    ↓
Signal Generation (GREEN/YELLOW/NO SIGNAL)
    ↓
Risk Management (Position Sizing, Heat Check)
    ↓
Paper Broker (Simulated Fills)
    ↓
Portfolio Ledger (SQLite)
    ↓
Dashboard/Alerts/Reports
```

## Directory Structure

```
trading_bot/
├── cli/app.py              # CLI commands
├── config/                 # Settings & loader
├── data/                   # Market data & indicators
├── strategy/               # Signal generation
│   ├── mean_reversion.py   # NEW
│   └── setup_rules.py
├── risk/                   # Position sizing
├── execution/              # Paper broker
├── portfolio/              # Ledger & P&L
├── monitoring/             # Health & metrics
│   ├── notifiers.py        # NEW: Webhooks
│   └── realtime_pnl.py     # NEW: Live P&L
├── runtime/                # Orchestrator
│   └── dashboard.py        # Enhanced
└── safety/                 # Kill switch
```

## Key Features

### Trading Strategies
- **Trend Following**: Breakout, Momentum continuation
- **Mean Reversion**: NEW - Oversold bounce, VWAP reversion, Range reversal

### Risk Controls
- Position cap: 20% per ticker
- Portfolio heat: 3% max unrealized loss
- Daily orders: 3 max
- Paper-only: Hardcoded false

### Monitoring
- Real-time P&L tracking
- Performance metrics (Win rate, Sharpe, Profit factor)
- Webhook alerts (Slack/Discord)
- Visual dashboard with charts

## Testing

285 tests covering:
- Unit tests for all modules
- Integration tests
- Network-free (monkeypatched)
- Deterministic

Run: `.venv/bin/python -m pytest -q`

## Configuration

Priority: Code defaults < config.yaml < Environment < CLI args

Critical settings:
- live_trading_enabled: false (HARDCODED)
- max_ticker_allocation: 0.20
- max_portfolio_heat: 0.03
