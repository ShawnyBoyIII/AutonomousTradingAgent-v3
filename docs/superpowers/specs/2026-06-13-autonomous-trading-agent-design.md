# Autonomous Trading Agent Design

## Goal

Build a brand-new Python trading system for stocks and ETFs that is safe by default, intraday-first, and fully paper-trading only in v1. The system must support both intraday and daily analysis, but intraday execution is the primary path. It must be auditable, modular, CLI-first, and designed so higher-fidelity data providers or broker adapters can be added later without rewriting core portfolio, risk, or strategy logic.

## Scope

### In scope for v1

- Stocks and ETFs only
- Paper trading only
- CLI commands for scan, paper trade, backtest, and report
- Intraday-first strategy flow using minute bars
- Daily timeframe support as a higher-timeframe trend filter
- Historical and near-current data via yfinance
- Technical indicators and signal generation
- Risk validation before every simulated order
- Portfolio accounting, order history, trade history, and P/L tracking
- CSV and JSON logs/reports
- Unit tests for safety-critical logic

### Out of scope for v1

- Live trading
- Crypto
- Options execution
- News scraping or sentiment models
- LLM-based multi-agent debate workflows
- Web dashboard
- Unofficial broker APIs
- Production-grade low-latency execution

## Product shape

User runs a CLI app locally. App can scan watchlists, generate signals, simulate paper orders, backtest strategies, and print or export performance summaries. All order creation flows through risk checks first. If a trade fails validation, system records rejection reason and takes no action.

## Architecture

### Repo layout

```text
AutonomousTradingAgent/
  README.md
  pyproject.toml
  .env.example
  config.yaml
  trading_bot/
    __init__.py
    main.py
    cli/
      __init__.py
      app.py
    config/
      __init__.py
      settings.py
      loader.py
    models/
      __init__.py
      market.py
      signal.py
      order.py
      portfolio.py
      risk.py
    data/
      __init__.py
      market_data.py
      indicators.py
      cache.py
    strategy/
      __init__.py
      intraday_signal_engine.py
      daily_filter.py
      setup_rules.py
    risk/
      __init__.py
      risk_manager.py
      position_sizer.py
      exposure.py
    portfolio/
      __init__.py
      ledger.py
      performance.py
    execution/
      __init__.py
      paper_broker.py
      order_manager.py
      fills.py
    backtest/
      __init__.py
      runner.py
      metrics.py
    reports/
      __init__.py
      exporters.py
      summaries.py
    utils/
      __init__.py
      logging.py
      timeframes.py
  tests/
    test_risk_manager.py
    test_position_sizer.py
    test_paper_broker.py
    test_strategy_signals.py
    test_backtest_runner.py
    test_cli_smoke.py
    test_live_safety.py
```

### Module responsibilities

- `config`: load YAML and env vars, enforce safety defaults
- `models`: shared typed data contracts
- `data`: fetch bars, normalize frames, compute indicators, cache results
- `strategy`: generate candidate setups from intraday bars, filtered by daily regime
- `risk`: reject or approve trades, size positions, cap exposure
- `portfolio`: maintain account state and realized/unrealized performance
- `execution`: simulate order lifecycle and fills
- `backtest`: replay historical data and measure performance
- `reports`: render CLI summaries and export files

## Core flows

### Scan flow

1. Load config and watchlist
2. Fetch daily and intraday bars for each ticker
3. Validate data freshness and minimum bar coverage
4. Compute indicators
5. Apply daily trend filter
6. Run intraday setup rules
7. Return ranked candidates with reasons and risk metadata

### Paper trading flow

1. Load account and portfolio state
2. Run scan flow
3. Convert strongest setups into candidate orders
4. Send each candidate through risk manager
5. Simulate approved orders through paper broker
6. Update positions, balances, and journal
7. Persist logs and summary output

### Backtest flow

1. Load ticker universe and historical date range
2. Replay bars chronologically
3. Generate only information available at each step
4. Apply risk checks before simulated execution
5. Record fills, equity curve, drawdown, and trade stats
6. Export metrics and trade journal

## Data design

### Market data inputs

- Daily OHLCV bars
- Intraday OHLCV minute bars
- Derived indicators:
  - EMA 9
  - EMA 20
  - SMA 50
  - SMA 200
  - RSI
  - MACD
  - ATR
  - volume moving average
  - support/resistance levels

### Data constraints

- Never fabricate missing bars
- Reject scan/backtest path when required columns are missing
- Mark stale or partial intraday data clearly
- Normalize timezone handling to US market session assumptions
- Cache raw fetches so repeated scans do not hammer provider unnecessarily

## Strategy design

### Primary approach

Intraday momentum/trend-continuation paper trader with daily confirmation.

Daily filter decides whether ticker is in acceptable regime:

- long bias when price is above key moving averages and trend is aligned
- optional no-trade regime when daily structure is weak or conflicting

Intraday engine searches for setups such as:

- pullback to short EMA in uptrend
- breakout above recent intraday range with volume confirmation
- momentum continuation after opening consolidation

### Signal output contract

Each signal must include:

- ticker
- timeframe context
- action: `BUY`, `SELL`, `HOLD`, `EXIT`
- entry price
- stop-loss
- profit target
- risk/reward ratio
- confidence score
- reason list
- strategy tag
- timestamp

### Strategy constraints

- No look-ahead bias
- No signal without stop-loss
- No signal if reward/risk is below configured minimum
- No duplicate signal for same ticker/setup within cooldown window

## Risk management

### Default rules

- Max risk per trade: 1% of account equity
- Max daily realized + open risk budget: 3% of equity
- Max single-ticker allocation: 20% of equity
- Minimum reward/risk: 2.0
- Reject orders with missing stop-loss
- Reject duplicate open-direction trades
- Optional config gate for restricted sessions or low liquidity periods

### Risk output

- approved: true/false
- rejection or approval reason
- position size
- estimated dollar risk
- estimated portfolio exposure after fill
- order instructions sent to execution layer

## Paper execution

### Behavior

- Supports market, limit, stop, and bracket-style simulated orders
- Uses configurable slippage and fees
- Fills market orders against most recent available bar context
- Updates cash, buying power, average cost, and realized/unrealized P/L
- Records every order event: submitted, accepted, partially filled if supported later, filled, cancelled, rejected

### Simplifications in v1

- No partial fill modeling in first pass
- No order book simulation
- No after-hours execution unless explicitly enabled later

## Portfolio accounting

Portfolio state stores:

- account equity
- cash
- buying power
- open positions
- average cost
- realized P/L
- unrealized P/L
- order history
- trade history
- daily equity snapshots

Persistence can be file-based in v1, using JSON or SQLite-backed storage. Preferred v1 choice: SQLite for transactions plus CSV export for human review.

## CLI design

### Commands

- `tradebot scan --symbols AAPL,MSFT,SPY`
- `tradebot paper-trade --symbols-file watchlist.txt`
- `tradebot backtest --symbols AAPL,SPY --start 2025-01-01 --end 2025-12-31`
- `tradebot report --date 2026-06-13`
- `tradebot portfolio`

### CLI output

- rich terminal tables for candidates, approvals, fills, and performance
- machine-readable JSON export option
- clear rejection reasons for blocked trades

## Configuration

### Static config

`config.yaml` holds:

- default watchlist
- timeframe settings
- indicator parameters
- risk limits
- slippage/fee assumptions
- market session settings
- cache settings

### Environment variables

- file paths
- optional override settings
- explicit future live-trading flags, present but disabled

Safety defaults:

- `LIVE_TRADING_ENABLED=false`
- no live broker credentials required in v1
- any future live mode path must fail closed unless explicit config and credentials exist

## Testing

### Unit tests

- position sizing math
- stop-loss required
- minimum reward/risk enforcement
- duplicate trade rejection
- max exposure rejection
- paper fill accounting
- backtest chronology correctness
- live mode disabled by default

### Smoke tests

- CLI command boots with sample config
- scan returns structured output for sample symbols
- backtest writes report artifacts successfully

## Logging and auditability

System logs:

- raw signal generation
- risk approval/rejection
- simulated order lifecycle
- portfolio updates
- errors and warnings

Artifacts:

- trade journal CSV
- order journal CSV
- daily performance summary JSON
- optional debug log file

## Extension path after v1

Designed extension points:

- swap `yfinance` with stronger provider later
- add broker adapter interface and live-safe placeholder
- add news/sentiment agent
- add LLM research agent layer on top of deterministic engine
- add web dashboard without replacing domain logic

## Recommended delivery sequence

1. Project scaffold and typed models
2. Config loader and logging
3. Market data fetch + indicators
4. Strategy signal engine
5. Risk manager
6. Paper broker and portfolio ledger
7. CLI commands
8. Backtest runner
9. Reports and exports
10. Test hardening

## Success criteria

V1 succeeds when user can:

- run a CLI command against chosen stock/ETF symbols
- generate intraday trade candidates with daily confirmation
- simulate approved trades safely
- backtest strategy without look-ahead bias
- inspect portfolio state and logs afterward
- trust that system never places a live trade in v1
