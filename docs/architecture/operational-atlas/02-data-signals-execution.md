# Phase 2 — Data, Signals, Risk, Execution

> 2026-07-29 snapshot at HEAD `62d178b`. All subsystems are exercised
> by the live burner's pre-market loop and confirmed via the
> verification matrix. No new defects found in this audit beyond the
> hypotheses queued in the remediation backlog.

## Pipeline

```text
provider registry -> provider adapters -> MarketDataCache (sqlite)
                                          |
                              validate_bars (V2.5 fail-fast)
                                          |
                              feature_pipeline + indicators (EMA/RSI/ATR/MACD/VWAP/Bollinger)
                                          |
                              strategy (V2.5 + V3 parallel consensus)
                                          |
                              counter_thesis + supermodel guard
                                          |
                              risk sizing (ATR + cap + portfolio heat)
                                          |
                              order_manager -> paper_broker
                                          |
                              FillResult -> fill_transaction
                                          |
                              orders / trades / positions / portfolio_state / equity_history
```

## Market data

### `trading_bot/data/providers/registry.py`
**Purpose:** Single source of truth for provider capabilities, network-free
credential readiness, and intraday fallback priority. Lists supported
intervals per provider; unsupported intervals are skipped before
provider construction.

**Tests:** `tests/test_provider_registry.py`. All pass.

### `trading_bot/data/providers/base.py`
**Purpose:** `MarketDataProvider` abstract base class with
`fetch_bars`, `validate_credentials`, `capabilities` interface.

**Tests:** `tests/test_provider_base.py` (if present; otherwise via
adapter tests).

### `trading_bot/data/providers/polygon_provider.py`
**Purpose:** Polygon.io adapter. Used as primary in `config.yaml` default.

### `trading_bot/data/providers/alpaca_provider.py`
**Purpose:** Alpaca adapter. Selected via `config.alpaca.yaml`.

**Tests:** `tests/test_alpaca_provider.py`. All pass.

### `trading_bot/data/providers/yfinance_provider.py`
**Purpose:** yfinance adapter. Fallback when Polygon/Alpaca unavailable.

**Tests:** `tests/test_yfinance_provider.py`. All pass.

### `trading_bot/data/providers/finnhub_provider.py`
**Purpose:** Finnhub adapter. Available but rarely configured.

### `trading_bot/data/market_data.py`
**Purpose:** `fetch_bars()` — the monkeypatch boundary used by tests.
Delegates to the provider registry.

### `trading_bot/data/cache.py`
**Purpose:** `MarketDataCache` SQLite-backed bar cache. Stores OHLCV
bars keyed by `(symbol, interval, ts)`.

**Known issue:** `FutureWarning` on mixed-timezone `pd.to_datetime` at
line 274; not a defect, just a deprecation ahead of `utc=True`.

### `trading_bot/data/data_store.py`
**Purpose:** Massive.com S3 flat-file long-term store for EOD bars.
Index, manifest, and parquet writers.

**Tests:** `tests/test_data_store.py`. All pass.

### `trading_bot/data/eod_fetcher.py`
**Purpose:** Massive.com S3 fetcher. Idempotent per `(date, intervals)`.

**Tests:** `tests/test_eod_fetcher.py`. All pass.

### `trading_bot/data/eod_runner.py`
**Purpose:** CLI glue for `eod-fetch` command.

### `trading_bot/data/indicators.py`
**Purpose:** EMA, SMA, RSI, ATR, MACD, Bollinger, VWAP, ADX, OBV
implementations.

**Tests:** `tests/test_indicators.py`. All pass.

### `trading_bot/data/feature_pipeline.py`
**Purpose:** Composable feature pipeline used by V3 signal engine.

**Tests:** `tests/test_feature_pipeline.py`. All pass.

### `trading_bot/data/validation.py`
**Purpose:** V2.5 fail-fast price/OHLC/volume sanity checks. Stops on
first validation error.

**Tests:** `tests/test_validation.py`. All pass.

## Strategy

### `trading_bot/strategy/daily_signal_engine.py`
**Purpose:** EOD signal generation for `daily_filter` and backtests.

**Tests:** `tests/test_daily_signal_engine.py`. All pass.

### `trading_bot/strategy/intraday_signal_engine.py`
**Purpose:** Intraday 5m-bar signal generation for `scan` and
`paper-trade`.

**Tests:** `tests/test_intraday_signal_engine.py`. All pass.

### `trading_bot/strategy/parallel_signal.py` (loaded via `app.signal_mode`)
**Purpose:** V3 + V2.5 consensus path; counter-thesis, supermodel veto,
one-source half-sizing.

**Tests:** `tests/test_parallel_signal.py`. All pass.

### `trading_bot/strategy/counter_thesis.py`
**Purpose:** Counter-thesis veto engine. `fetch_counter_thesis_context`
is the only network-touching entry; `_check_*` are pure functions of
`(context, settings)`.

**Tests:** `tests/test_counter_thesis.py`. All pass.

### `trading_bot/strategy/supermodel.py`
**Purpose:** Allowlist-gated supermodel veto / support / block / counter
weights. Tunable via tuning experiments.

**Tests:** `tests/test_supermodel.py`. All pass.

### `trading_bot/strategy/strategy_selector.py`
**Purpose:** Regime + confluence scoring.

**Tests:** `tests/test_strategy_selector.py`. All pass.

### `trading_bot/strategy/signal_confluence.py`
**Purpose:** Multi-source confluence scoring used by V3 consensus.

### `trading_bot/strategy/signal_quality.py`
**Purpose:** GREEN/YELLOW/NO_SIGNAL classification; quality metrics
recorded on fills for attribution.

### `trading_bot/strategy/strategy_tracker.py`
**Purpose:** Rolling performance tracker with `window`, `min_win_rate`,
`full_allocation_rate` knobs. Records strategy tags on buys/sells.

### `trading_bot/strategy/market_regime.py`
**Purpose:** Market regime classifier.

### `trading_bot/strategy/mean_reversion.py`
**Purpose:** Mean-reversion strategy primitive.

### `trading_bot/strategy/trailing_stop.py`
**Purpose:** Symmetric trailing-stop ratchet logic used by both CLI
and continuous loop.

**Tests:** `tests/test_trailing_stop.py` family. All pass.

### `trading_bot/strategy/daily_filter.py`
**Purpose:** Pre-trade daily filter for the orchestrator.

### `trading_bot/strategy/news_filter.py`
**Purpose:** News-driven veto. Currently a stub that defers to
counter-thesis.

### `trading_bot/strategy/sector_rotation.py`
**Purpose:** Sector-rotation helper used by universe building.

### `trading_bot/strategy/setup_rules.py`
**Purpose:** Rule definitions consumed by both V2.5 and V3 engines.

### `trading_bot/strategy/dynamic_watchlist.py`
**Purpose:** Watchlist builder; emits final symbol without trailing
newline.

**Tests:** `tests/test_discover_failure_visibility.py`,
`tests/test_dynamic_watchlist.py`. All pass.

### `trading_bot/strategy/market_screener.py`
**Purpose:** Market-wide screener used by discovery (`discover --mode breakout`).

**Tests:** `tests/test_market_screener_discovery.py`. All pass.

## Risk

### `trading_bot/risk/position_sizer.py`
**Purpose:** ATR + capped position sizing. Enforces
`max_ticker_allocation_pct`, `max_risk_per_trade_pct`, and the hard
`max_shares_per_position` cap.

**Tests:** `tests/test_position_sizer.py`. All pass.

### `trading_bot/risk/risk_manager.py`
**Purpose:** Higher-level risk evaluation: portfolio heat, daily
order count, consecutive losses, ticker re-entry cooldown, ticker
allocation caps.

**Tests:** `tests/test_risk_manager.py`. All pass.

### `trading_bot/risk/correlation.py`
**Purpose:** Cross-symbol correlation computation. Used by the
portfolio heat aggregation in paper runtime.

### `trading_bot/risk/var.py`
**Purpose:** Value-at-Risk helper. Currently a stub exposed for future
integration.

**Status:** statically wired.

## Execution

### `trading_bot/execution/order_manager.py`
**Purpose:** Validates order_request against risk manager, calls
`paper_broker.submit_order`, returns the durable fill result.

### `trading_bot/execution/paper_broker.py`
**Purpose:** Simulated fill model with configured slippage/fees.

**Tests:** `tests/test_paper_broker.py`. All pass.

### `trading_bot/execution/fills.py`
**Purpose:** `FillResult`, `OrderRequest`, fee helpers.

### `trading_bot/execution/modes.py`
**Purpose:** Order-mode enum (`paper`, `live`). Live mode is forced
disabled by `live_trading_enabled=False` in the loader.

### `trading_bot/execution/broker_base.py`
**Purpose:** `BrokerAdapter` abstract base for Robinhood MCP and
future adapters.

### `trading_bot/runtime/fill_transaction.py`
**Purpose:** Atomic two-step transaction: `build_buy_transaction` and
`build_sell_transaction`. Adds optional `runtime_canary` kwargs that
thread the canary_experiment_id and baseline quantity into the
durable `orders` row.

**Tests:** `tests/test_fill_transaction.py`. All pass.

## Models

### `trading_bot/models/order.py`
**Purpose:** `OrderRequest`, `FillResult` dataclasses.

### `trading_bot/models/market.py`
**Purpose:** `Bar`, `Quote` typed models.

### `trading_bot/models/portfolio.py`
**Purpose:** `Position`, `PortfolioState` typed models.

### `trading_bot/models/risk.py`
**Purpose:** Typed risk limits.

### `trading_bot/models/signal.py`
**Purpose:** `Signal`, `ApprovedCandidate` typed models.

### `trading_bot/models/scout.py`
**Purpose:** Scout discovery output types.

## Cross-references

- `trading_bot/data/providers/registry.py` is the source of truth for
  provider capabilities and intraday fallback priority. Any new
  provider must be added there in the same commit as its adapter.
- `trading_bot/strategy/counter_thesis.py::fetch_counter_thesis_context`
  is the only network-touching entry in the strategy layer.
- `trading_bot/execution/paper_broker.py::submit_order` is the only
  caller of `FillResult` construction in the live runtime.
- `trading_bot/runtime/fill_transaction.py::build_*_transaction` is
  the durable write boundary that pairs the canary metadata.
