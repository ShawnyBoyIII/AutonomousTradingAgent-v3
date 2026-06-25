# AGENTS.md - Trading Bot

Critical context for OpenCode sessions.

---

## Entry Point

**Always use the repo-local wrapper:**
```bash
./tradebot-local <command> [options]
./tradebot-local --config-path custom.yaml <command>
```

Never use bare `tradebot` on PATH—it may resolve to a stale global install.

---

## Safety Constraints (Hard Rules)

1. **Paper-only by default** - `live_trading_enabled` forced `False` in `config/loader.py:75`
2. **Never modify tests** when fixing bugs - tests are source of truth
3. **All tests must be network-free** - monkeypatch `fetch_bars`, use `monkeypatch` fixtures
4. **Position sizing capped at 20%** - `max_ticker_allocation_pct=0.20`
5. **Kill switch blocks all trading** - integrated at entry points before any logic
6. **No hardcoded credentials** - loader rejects config files with passwords/api keys
7. **Robinhood is MCP-only** - no direct auth; boundary subclasses `BrokerAdapter`, reads operator-synced JSON snapshots

---

## Testing

```bash
.venv/bin/python -m pytest -q          # 650 tests
.venv/bin/python -m pytest tests/test_kill_switch.py -v
.venv/bin/python -m pytest tests/test_kill_switch.py::test_kill_switch_status -v
```

**Requirements:**
- Tests are deterministic: monkeypatch market data, use `tmp_path` fixtures
- No real network calls
- Config: `pytest -ra --strict-markers` (pyproject.toml)

---

## Architecture

```
trading_bot/
├── cli/app.py           # Typer CLI commands
├── config/              # Settings, loader (YAML → Pydantic)
├── data/                # Market data, validation, indicators
│   ├── market_data.py   # fetch_bars() - monkeypatch this
│   └── validation.py    # V2.5: price/OHLC/volume validation
├── execution/           # Order management
│   └── paper_broker.py  # Simulated fills with slippage/fees
├── brokers/robinhood/   # MCP boundary + legacy code
├── portfolio/           # Ledger, P&L tracking
├── risk/                # Position sizing (ATR + 20% cap)
├── strategy/            # Signals, V3 counter-thesis
├── safety/kill_switch.py
└── runtime/orchestrator.py
```

**Data flow:**
1. `scan` → checks universe for GREEN signals
2. `paper-trade` → generates orders, simulates fills
3. `manage-positions` → checks stops, targets, EOD exits
4. All entry points check kill switch first

---

## Configuration

**Critical defaults:**
```yaml
app:
  live_trading_enabled: false  # Always false, enforced

risk:
  max_risk_per_trade_pct: 0.01
  max_ticker_allocation_pct: 0.20  # 20% max
  max_portfolio_heat_pct: 0.03     # Blocks at 3% unrealized loss

market_data:
  validate_data: true  # V2.5: fail-fast
```

**Quirks:**
- Paths resolved relative to config file location
- Credentials via environment variables or `.env` only

---

## Common Commands

```bash
./tradebot-local doctor                    # Health check
./tradebot-local scan --symbols SPY,AAPL --why --summary
./tradebot-local paper-trade --symbols AAPL
./tradebot-local manage-positions
./tradebot-local portfolio
./tradebot-local kill-switch --status|--halt|--resume
./tradebot-local backtest --start-date 2025-01-01 --end-date 2025-06-01

# V3: Counter-thesis (opt-in via config)
./tradebot-local counter-thesis --symbols AAPL --why

# V3: Robinhood MCP (read-only)
./tradebot-local robinhood-status
./tradebot-local sync-positions

# Burn-in automation
./scripts/auto-burn-in.sh
tail -f logs/burn_in/decision-log.jsonl
```

---

## V3 Strategy Layer (Feature Flag)

**Enable in config:**
```yaml
strategy:
  use_v3_signals: true      # Default: false
  risk_tolerance: medium
  min_confidence: medium

counter_thesis:
  enabled: true             # Default: false
  block_on_severity: high
  aggregate_block_threshold: 0.6
  exit_on_block: true
```

**Behavior:**
- `scan --why` surfaces `v3_total_score`, `v3_confidence`, `v3_regime`
- Counter-thesis vetoes trades or scales position by `confidence_multiplier`
- `manage-positions` exits when thesis broken (before trailing stop)
- Backtest runner evaluates counter-thesis on each bar (no network)

**Design:** `fetch_counter_thesis_context` is the only network-touching entry; all `_check_*` are pure functions of `(context, settings)`.

---

## Exit Priority (ADR-001)

1. **EOD exit** (highest - always exit before close)
2. **Stop loss**
3. **Profit target**
4. **Counter-thesis exit** (V3: thesis broken)
5. **Trailing stop** (lowest)

---

## Data Validation (V2.5)

All market data validated before use:
1. Price sanity: 0 < price < 10x jump from previous
2. OHLC coherence: high ≥ close ≥ low
3. Volume sanity: reasonable levels

Fail-fast: stops on first validation error.

---

## Key Files

- `trading_bot/cli/app.py` - CLI commands
- `trading_bot/config/loader.py` - Config loading + safety enforcement
- `trading_bot/runtime/orchestrator.py` - V2.5 + V3 signal paths
- `trading_bot/strategy/counter_thesis.py` - Counter-thesis engine
- `trading_bot/strategy/strategy_selector.py` - Regime + confluence
- `trading_bot/risk/position_sizer.py` - ATR sizing logic
- `docs/V2_5_PHASE_D_BURN_IN_GUIDE.md` - Operational guide

---

## Current State

- **650 tests passing**
- **V2.5 complete:** ATR sizing, validation, kill switches, burn-in
- **V3 wired:** Regime detection, confluence scoring, counter-thesis (entry + exit + backtest)
- **Phase D active:** Running paper burn-in with dynamic watchlist
