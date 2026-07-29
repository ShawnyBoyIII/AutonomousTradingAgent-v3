# File Coverage Index

> 185 operational files. Each row shows path and subsystem layer; phase
> documents describe callers, side effects, and status per row.

## Entrypoints and runtime (11)

| Path | Layer |
| --- | --- |
| `scripts/auto-burn-in.sh` | entry |
| `scripts/auto_bench_cron.py` | entry |
| `scripts/burn-in-monitor.sh` | entry |
| `scripts/burn-in-weekly-review.sh` | entry |
| `scripts/burnin-launcher.sh` | entry |
| `scripts/daily-start.sh` | entry |
| `scripts/security-harden.sh` | entry |
| `scripts/start-dashboard.sh` | entry |
| `tradebot-local` | entry |
| `trading_bot/cli/__init__.py` | entry |
| `trading_bot/cli/app.py` | entry |

## Configuration (6)

| Path | Layer |
| --- | --- |
| `burn-in-config.yaml` | config |
| `config.alpaca.yaml` | config |
| `config.yaml` | config |
| `rl-config-example.yaml` | config |
| `trading_bot/config/loader.py` | config |
| `trading_bot/config/settings.py` | config |

## Market data and indicators (15)

| Path | Layer |
| --- | --- |
| `trading_bot/data/cache.py` | data |
| `trading_bot/data/data_store.py` | data |
| `trading_bot/data/eod_fetcher.py` | data |
| `trading_bot/data/eod_runner.py` | data |
| `trading_bot/data/feature_pipeline.py` | data |
| `trading_bot/data/indicators.py` | data |
| `trading_bot/data/market_data.py` | data |
| `trading_bot/data/providers/__init__.py` | data |
| `trading_bot/data/providers/alpaca_provider.py` | data |
| `trading_bot/data/providers/base.py` | data |
| `trading_bot/data/providers/finnhub_provider.py` | data |
| `trading_bot/data/providers/polygon_provider.py` | data |
| `trading_bot/data/providers/registry.py` | data |
| `trading_bot/data/providers/yfinance_provider.py` | data |
| `trading_bot/data/validation.py` | data |

## Strategy and signals (24)

| Path | Layer |
| --- | --- |
| `trading_bot/factors/__init__.py` | strategy |
| `trading_bot/factors/bench.py` | strategy |
| `trading_bot/patterns/__init__.py` | strategy |
| `trading_bot/patterns/digest.py` | strategy |
| `trading_bot/patterns/miner.py` | strategy |
| `trading_bot/sentiment/__init__.py` | strategy |
| `trading_bot/sentiment/context.py` | strategy |
| `trading_bot/strategy/counter_thesis.py` | strategy |
| `trading_bot/strategy/daily_filter.py` | strategy |
| `trading_bot/strategy/daily_signal_engine.py` | strategy |
| `trading_bot/strategy/dynamic_watchlist.py` | strategy |
| `trading_bot/strategy/intraday_signal_engine.py` | strategy |
| `trading_bot/strategy/market_regime.py` | strategy |
| `trading_bot/strategy/market_screener.py` | strategy |
| `trading_bot/strategy/mean_reversion.py` | strategy |
| `trading_bot/strategy/news_filter.py` | strategy |
| `trading_bot/strategy/sector_rotation.py` | strategy |
| `trading_bot/strategy/setup_rules.py` | strategy |
| `trading_bot/strategy/signal_confluence.py` | strategy |
| `trading_bot/strategy/signal_quality.py` | strategy |
| `trading_bot/strategy/strategy_selector.py` | strategy |
| `trading_bot/strategy/strategy_tracker.py` | strategy |
| `trading_bot/strategy/supermodel.py` | strategy |
| `trading_bot/strategy/trailing_stop.py` | strategy |

## Risk management (4)

| Path | Layer |
| --- | --- |
| `trading_bot/risk/correlation.py` | risk |
| `trading_bot/risk/position_sizer.py` | risk |
| `trading_bot/risk/risk_manager.py` | risk |
| `trading_bot/risk/var.py` | risk |

## Order execution (6)

| Path | Layer |
| --- | --- |
| `trading_bot/execution/__init__.py` | execution |
| `trading_bot/execution/broker_base.py` | execution |
| `trading_bot/execution/fills.py` | execution |
| `trading_bot/execution/modes.py` | execution |
| `trading_bot/execution/order_manager.py` | execution |
| `trading_bot/execution/paper_broker.py` | execution |

## Portfolio, ledger, and DB (14)

| Path | Layer |
| --- | --- |
| `trading_bot/db/__init__.py` | persistence |
| `trading_bot/db/models.py` | persistence |
| `trading_bot/db/repositories/__init__.py` | persistence |
| `trading_bot/db/repositories/events.py` | persistence |
| `trading_bot/db/repositories/market_data.py` | persistence |
| `trading_bot/db/repositories/model_predictions.py` | persistence |
| `trading_bot/db/repositories/portfolio_snapshots.py` | persistence |
| `trading_bot/db/repositories/positions.py` | persistence |
| `trading_bot/db/repositories/scan_features.py` | persistence |
| `trading_bot/db/repositories/scan_results.py` | persistence |
| `trading_bot/db/repositories/trades.py` | persistence |
| `trading_bot/db/session.py` | persistence |
| `trading_bot/portfolio/ledger.py` | persistence |
| `trading_bot/portfolio/performance.py` | persistence |

## Reports and analytics (6)

| Path | Layer |
| --- | --- |
| `trading_bot/analytics/__init__.py` | analytics |
| `trading_bot/analytics/evaluation_windows.py` | analytics |
| `trading_bot/analytics/paper_performance.py` | analytics |
| `trading_bot/reports/burn_in_analytics.py` | analytics |
| `trading_bot/reports/exporters.py` | analytics |
| `trading_bot/reports/summaries.py` | analytics |

## Dashboard (4)

| Path | Layer |
| --- | --- |
| `ui/dashboard/main.py` | dashboard |
| `ui/dashboard/static/css/dashboard.css` | dashboard |
| `ui/dashboard/static/js/dashboard.js` | dashboard |
| `ui/dashboard/templates/dashboard.html` | dashboard |

## Safety gates (3)

| Path | Layer |
| --- | --- |
| `trading_bot/safety/__init__.py` | safety |
| `trading_bot/safety/circuit_breaker.py` | safety |
| `trading_bot/safety/kill_switch.py` | safety |

## Monitoring and doctor (10)

| Path | Layer |
| --- | --- |
| `trading_bot/health/__init__.py` | monitoring |
| `trading_bot/health/checks.py` | monitoring |
| `trading_bot/health/runner.py` | monitoring |
| `trading_bot/health/types.py` | monitoring |
| `trading_bot/monitoring/__init__.py` | monitoring |
| `trading_bot/monitoring/drawdown.py` | monitoring |
| `trading_bot/monitoring/health.py` | monitoring |
| `trading_bot/monitoring/notifiers.py` | monitoring |
| `trading_bot/monitoring/performance.py` | monitoring |
| `trading_bot/monitoring/realtime_pnl.py` | monitoring |

## Learning, memory, and tuning (14)

| Path | Layer |
| --- | --- |
| `trading_bot/learning/__init__.py` | learning |
| `trading_bot/learning/experiments/__init__.py` | learning |
| `trading_bot/learning/experiments/controller.py` | learning |
| `trading_bot/learning/experiments/models.py` | learning |
| `trading_bot/learning/experiments/proposal.py` | learning |
| `trading_bot/learning/experiments/replay.py` | learning |
| `trading_bot/learning/experiments/runtime_canary.py` | learning |
| `trading_bot/learning/experiments/shadow.py` | learning |
| `trading_bot/learning/experiments/store.py` | learning |
| `trading_bot/learning/tuning_overrides.py` | learning |
| `trading_bot/memory/__init__.py` | learning |
| `trading_bot/memory/models.py` | learning |
| `trading_bot/memory/retriever.py` | learning |
| `trading_bot/memory/store.py` | learning |

## Advisory learner (4)

| Path | Layer |
| --- | --- |
| `trading_bot/advisory/__init__.py` | advisory |
| `trading_bot/advisory/learner.py` | advisory |
| `trading_bot/advisory/models.py` | advisory |
| `trading_bot/advisory/reporting.py` | advisory |

## Research and event engine (23)

| Path | Layer |
| --- | --- |
| `event_engine/__init__.py` | research |
| `event_engine/analytics.py` | research |
| `event_engine/engine.py` | research |
| `event_engine/events.py` | research |
| `event_engine/exceptions.py` | research |
| `event_engine/execution.py` | research |
| `event_engine/handlers.py` | research |
| `event_engine/portfolio.py` | research |
| `event_engine/prefilter.py` | research |
| `event_engine/queue.py` | research |
| `event_engine/strategy.py` | research |
| `examples/event_engine_analytics.py` | research |
| `trading_bot/research/__init__.py` | research |
| `trading_bot/research/benching_weights.py` | research |
| `trading_bot/research/engine.py` | research |
| `trading_bot/research/models.py` | research |
| `trading_bot/research/store.py` | research |
| `trading_bot/swarm/__init__.py` | research |
| `trading_bot/swarm/base.py` | research |
| `trading_bot/swarm/engine.py` | research |
| `trading_bot/swarm/presets.py` | research |
| `trading_bot/swarm/results.py` | research |
| `trading_bot/swarm/workers.py` | research |

## Broker adapters (Robinhood MCP) (5)

| Path | Layer |
| --- | --- |
| `trading_bot/brokers/__init__.py` | broker |
| `trading_bot/brokers/base.py` | broker |
| `trading_bot/brokers/robinhood/__init__.py` | broker |
| `trading_bot/brokers/robinhood/boundary.py` | broker |
| `trading_bot/brokers/robinhood/reconciliation.py` | broker |

## Legacy event primitives (6)

| Path | Layer |
| --- | --- |
| `trading_bot/events/__init__.py` | events |
| `trading_bot/events/bus.py` | events |
| `trading_bot/events/cache.py` | events |
| `trading_bot/events/loop.py` | events |
| `trading_bot/events/orchestrator.py` | events |
| `trading_bot/events/types.py` | events |

## Runtime orchestration (15)

| Path | Layer |
| --- | --- |
| `trading_bot/env/__init__.py` | runtime |
| `trading_bot/runtime/__init__.py` | runtime |
| `trading_bot/runtime/approved_candidate.py` | runtime |
| `trading_bot/runtime/burnin_pin.py` | runtime |
| `trading_bot/runtime/continuous_loop.py` | runtime |
| `trading_bot/runtime/decision_log.py` | runtime |
| `trading_bot/runtime/fill_transaction.py` | runtime |
| `trading_bot/runtime/latency.py` | runtime |
| `trading_bot/runtime/mark_to_market.py` | runtime |
| `trading_bot/runtime/orchestrator.py` | runtime |
| `trading_bot/runtime/position_exit.py` | runtime |
| `trading_bot/runtime/position_management.py` | runtime |
| `trading_bot/runtime/session.py` | runtime |
| `trading_bot/runtime/snapshots.py` | runtime |
| `trading_bot/runtime/watchlist.py` | runtime |

## Data models (7)

| Path | Layer |
| --- | --- |
| `trading_bot/models/__init__.py` | models |
| `trading_bot/models/market.py` | models |
| `trading_bot/models/order.py` | models |
| `trading_bot/models/portfolio.py` | models |
| `trading_bot/models/risk.py` | models |
| `trading_bot/models/scout.py` | models |
| `trading_bot/models/signal.py` | models |

## Backtest (4)

| Path | Layer |
| --- | --- |
| `trading_bot/backtest/attribution.py` | backtest |
| `trading_bot/backtest/diagnostics.py` | backtest |
| `trading_bot/backtest/metrics.py` | backtest |
| `trading_bot/backtest/runner.py` | backtest |

## Trading bot root and helpers (4)

| Path | Layer |
| --- | --- |
| `trading_bot/__init__.py` | trading_bot_root |
| `trading_bot/logging_config.py` | trading_bot_root |
| `trading_bot/main.py` | trading_bot_root |
| `trading_bot/scout.py` | trading_bot_root |
