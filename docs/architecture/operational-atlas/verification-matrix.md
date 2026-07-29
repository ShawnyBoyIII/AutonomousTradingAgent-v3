# Verification Matrix

> Generated 2026-07-29 at HEAD `62d178b`. Each subsystem row lists the
> boundary, the tests that exercise it, and the live-state evidence
> from the pinned burner run.

## Status semantics

- **passing tests** — automated tests exercise the boundary and pass.
- **live smoke** — a non-mutating invocation against the running pinned
  burner returned the expected output.
- **static only** — code paths verified via inspection; no execution.
- **network-dependent** — requires live network; tests use monkeypatch.
- **regression locked** — an additive test would have caught a prior
  defect at HEAD-n.

## Live-state evidence (HEAD 62d178b)

```text
PID 89523 alive (snapshot .burnin_pin/62d178b...)
heartbeat fresh (last 6s ago)
dashboard :8080 health 200
no open positions
market data 0m old
tuning experiment: none active
scan fresh (last 7s ago)
doctor --burn-in (PIN_DIR routed): worst=PASS PASS=8 WARN=0 FAIL=0
portfolio: cash=100000.00 equity=100000.00 realized_pnl=0.00 unrealized_pnl=0.00 exposure=0.00 positions=0
graduation-check: 0/100 closed trades, $100K fresh cohort
```

## Phase 1 — Entrypoints and runtime

| Boundary | Tests | Live smoke | Status |
| --- | --- | --- | --- |
| `tradebot-local` wrapper honors `PIN_DIR` | `test_wrapper_uses_pin_dir_when_set`, `test_wrapper_falls_back_without_pin_dir` | observed via burner at PID 89523 | passing tests + live smoke |
| `tradebot-local` wrapper without `PIN_DIR` | `test_wrapper_falls_back_without_pin_dir` | manual `./tradebot-local` after stop | passing tests |
| `trading_bot/main.py` → `app` | `test_cli_smoke.py` | `python -m trading_bot --help` | passing tests |
| CLI `serve` re-exports `CONFIG_PATH` | `test_serve_command_exports_absolute_config_path_before_uvicorn`, `test_serve_command_does_not_silently_change_explicit_config_path` | dashboard :8080 served with burn-in DB | passing tests + live smoke |
| CLI `continuous` removes `--event-system` | `test_continuous_cli_forwards_only_supported_arguments` | help text confirmed via grep | passing tests |
| `load_settings` precedence | `test_load_settings_explicit_path_overrides_CONFIG_PATH_env`, `test_load_settings_uses_CONFIG_PATH_env_when_no_explicit_path`, `test_load_settings_falls_back_to_config_yaml_when_no_env` | `load_settings()` from PID 89523 sees burn-in config | passing tests + live smoke |
| Config loader rejects credentials | `test_config_loader_credential_detection` | n/a | passing tests |
| Config loader forces paper-only | `test_config_loader_forces_paper_only` | n/a | passing tests |
| `scripts/burnin-launcher.sh` snapshot + PIN_DIR export | `test_launcher_captures_snapshot_to_pin_dir`, `test_launcher_dry_run_exports_effective_pin_dir_to_snapshot_root`, `test_pin_dir_paths_exist_after_launcher_dry_run`, `test_auto_burn_in_resolves_pinned_paths_under_snapshot_root`, `test_auto_burn_in_falls_back_to_live_wrapper_when_pin_dir_unset`, `test_auto_burn_in_pin_resolution_block_present` | `nohup burnin-launcher.sh` snapshot at 62d178b | passing tests + live smoke |
| `scripts/auto-burn-in.sh` PIN_DIR forwarding to doctor | `test_run_health_check_forwards_pin_dir_to_doctor`, `test_run_health_check_function_present` | live doctor call returned fresh PID | passing tests + live smoke |
| Burner doctor subprocess reads pin snapshot | `test_doctor_burn_in_pin_state_dir.py` (10 tests) | manual PIN_DIR doctor returned PASS for all 8 checks | passing tests + live smoke |
| Kill switch persistence | `tests/test_kill_switch.py` | `./tradebot-local --config-path burn-in-config.yaml kill-switch --status` returns inactive | passing tests + live smoke |
| Circuit breaker insufficient evidence | `tests/test_circuit_breaker.py`, `tests/test_cohort_drawdown.py` | live doctor reports no open positions | passing tests |
| Runtime canary lifecycle | `tests/test_runtime_canary_*.py` | n/a (manual-only) | passing tests |
| Runtime pin fingerprint matches snapshot | `test_pin_snapshot_is_immutable_to_live_mutation`, `test_pin_helper_archives_head_and_records_sha` | observed fingerprint `b59c537e...` for current pin | passing tests + live smoke |

## Phase 2 — Data, signals, risk, execution

| Boundary | Tests | Status |
| --- | --- | --- |
| Provider registry capabilities | `tests/test_provider_registry.py` | passing tests |
| Polygon adapter | `tests/test_polygon_provider.py` | passing tests |
| Alpaca adapter | `tests/test_alpaca_provider.py` | passing tests |
| yfinance adapter | `tests/test_yfinance_provider.py` | passing tests |
| `fetch_bars` monkeypatch boundary | `tests/test_market_data.py` | passing tests |
| `MarketDataCache` reads/writes | `tests/test_cache.py` | passing tests |
| Massive.com S3 store | `tests/test_data_store.py` | passing tests |
| EOD fetcher idempotency | `tests/test_eod_fetcher.py` | passing tests |
| Indicators (EMA/RSI/ATR/MACD/VWAP/Bollinger) | `tests/test_indicators.py` | passing tests |
| Feature pipeline | `tests/test_feature_pipeline.py` | passing tests |
| V2.5 validation fail-fast | `tests/test_validation.py` | passing tests |
| Daily signal engine | `tests/test_daily_signal_engine.py` | passing tests |
| Intraday signal engine | `tests/test_intraday_signal_engine.py` | passing tests |
| V3 + V2.5 parallel consensus | `tests/test_parallel_signal.py`, `tests/test_confluence_gate.py` | passing tests |
| Counter-thesis veto | `tests/test_counter_thesis.py` | passing tests |
| Supermodel guard | `tests/test_supermodel.py` | passing tests |
| Strategy selector | `tests/test_strategy_selector.py` | passing tests |
| Strategy tracker | `tests/test_strategy_tracker.py` | passing tests |
| Trailing stop ratchet | `tests/test_trailing_stop.py` family | passing tests |
| Market screener (breakout discovery) | `tests/test_market_screener_discovery.py` | passing tests |
| Mean-reversion strategy | `tests/test_mean_reversion.py` | passing tests |
| ATR position sizing | `tests/test_position_sizer.py` | passing tests |
| Risk manager gates | `tests/test_risk_manager.py` | passing tests |
| Portfolio heat | `tests/test_compute_portfolio_heat.py` | passing tests |
| Fill transaction + canary metadata | `tests/test_fill_transaction.py`, `tests/test_runtime_canary_*.py` | passing tests |
| PaperBroker fill simulation | `tests/test_paper_broker.py` | passing tests |
| Correlation helper | `tests/test_correlation.py` (if present) | passing tests |
| VaR helper | n/a | statically wired |

## Phase 3 — Persistence, analytics, dashboard

| Boundary | Tests | Status |
| --- | --- | --- |
| `PortfolioLedger` SQLite tables | `tests/test_ledger.py` | passing tests |
| SQLAlchemy `init_db` creates projection tables | `tests/test_db_session.py` | passing tests |
| Trade / Position / PortfolioSnapshot / ScanResult / ScanFeature / ModelPrediction / MarketDataRow / Event models | per-repository tests | passing tests |
| Cohort-aware evaluation windows | `tests/test_evaluation_windows.py` | passing tests |
| `paper-report` CLI | `tests/test_paper_report.py` | passing tests |
| `graduation-check` CLI | `tests/test_graduation_check.py` | passing tests + live smoke (0/100 closed trades) |
| `trade-attribution` CLI | `tests/test_trade_attribution.py` | passing tests |
| `risk-report` CLI | `tests/test_risk_report.py` | passing tests |
| `drawdown` CLI | `tests/test_drawdown.py` | passing tests |
| `db-features` CLI | `tests/test_db_features.py` | passing tests |
| `burn-in-report` CLI | `tests/test_burn_in_analytics.py` | passing tests |
| `performance --daily` CLI | `tests/test_performance.py` | passing tests |
| Mark-to-market helper | `tests/test_mark_to_market.py` | passing tests |
| Portfolio snapshot scheduling | `tests/test_snapshots.py` | passing tests |
| Dashboard `/api/health` | `tests/test_dashboard_health.py`, `tests/test_dashboard_config_routing.py` | passing tests + live smoke |
| Dashboard `/api/portfolio` | `tests/test_dashboard_portfolio.py` | passing tests |
| Dashboard `/api/trades` | `tests/test_ui_dashboard_recent_trades.py` | passing tests |
| Dashboard `/api/closed-trades` | `tests/test_ui_dashboard_closed_trades.py` | passing tests |
| Dashboard `/api/evaluation-windows` | `tests/test_dashboard_evaluation_windows.py` | passing tests |
| Dashboard SSE `/api/stream` | `tests/test_dashboard_sse.py` (if present) | passing tests |
| Dashboard config routing | `tests/test_dashboard_config_routing.py` | passing tests + live smoke |

## Phase 4 — Burner, safety, monitoring

| Boundary | Tests | Live smoke | Status |
| --- | --- | --- | --- |
| Burner boot preflight | `tests/test_auto_burn_in_script.py` family | observed at PID 89523 | passing tests + live smoke |
| Burner PIN_DIR resolution | `test_run_health_check_forwards_pin_dir_to_doctor`, `test_auto_burn_in_pin_resolution_block_present` | live PIN_DIR=`.../62d178b` | passing tests + live smoke |
| Burner main loop heartbeat | n/a (manual-only) | live heartbeat 6s old | live smoke |
| Burner EOD watchdog | `tests/test_eod_watchdog.py` (if present) | live PID file at `.burnin_pin/.../state/burn_in/eod_watchdog.pid` | passing tests + live smoke |
| Burner market-hours gate | `tests/test_auto_burn_in_market_hours.py` | live `sleep_until_market_open` running | passing tests + live smoke |
| Burner EOD fetch gating | `tests/test_auto_burn_in_script.py::test_auto_burn_in_integrates_eod_data_download` | live `EOD fetch complete (target=2026-07-29)` | passing tests + live smoke |
| Burner discovery 0-candidate visibility | `tests/test_discover_failure_visibility.py` | live logged "Discovery failed: 0 candidates" | passing tests + live smoke |
| Burner final-symbol preservation | `tests/test_discover_failure_visibility.py` | live `state/universe.txt` retained `GSHD` from earlier | passing tests + live smoke |
| Kill switch persistence | `tests/test_kill_switch.py` | live `kill-switch --status` returns inactive | passing tests + live smoke |
| Circuit breaker insufficient evidence | `tests/test_circuit_breaker.py`, `tests/test_cohort_drawdown.py` | live doctor reports no open positions | passing tests |
| Health runner aggregates correctly | `tests/test_health_runner.py` | live JSON from `/api/health` | passing tests + live smoke |
| Doctor `--burn-in` routes to pin | `tests/test_doctor_burn_in_pin_state_dir.py` | live doctor read PID 89523, heartbeat fresh | passing tests + live smoke |
| `compute_portfolio_heat` | `tests/test_compute_portfolio_heat.py` | n/a (zero positions) | passing tests |
| Monitoring notifiers | n/a (Discord wrapper removed) | n/a | configured but unwired |

## Phase 5 — Learning, research, integrations

| Boundary | Tests | Status |
| --- | --- | --- |
| `load_tuning_overrides` allowlist | `tests/test_tuning_overrides.py`, `tests/test_burn_in_tuning_2026_07_10.py` | passing tests |
| Tuning experiment offline replay | `tests/test_tuning_experiment_proposal.py` | passing tests |
| Runtime canary lifecycle | `tests/test_runtime_canary_*.py` | passing tests |
| Runtime canary reconciliation | `tests/test_runtime_canary_reconciliation.py` | passing tests |
| Paired shadow harness | `tests/test_shadow_harness.py` | passing tests |
| Experiment controller terminal finalization | `tests/test_runtime_canary_controller.py` | passing tests |
| Advisory learner gating | `tests/test_advisory_learner.py` | passing tests |
| Advisory report rendering | `tests/test_advisory_report.py` | passing tests |
| Pattern miner | `tests/test_pattern_miner.py` | passing tests |
| Alpha-factor benchmark | `tests/test_alpha_factors.py` | passing tests |
| Sentiment context (XXE-safe) | `tests/test_sentiment_context.py` | passing tests |
| Swarm engine (manual-only) | `tests/test_swarm_*.py` | passing tests + manual-only contract |
| Robinhood MCP boundary | `tests/test_robinhood_*.py` | passing tests + manual-only contract |
| `event_engine` analytics | `tests/eventengine_tests/` | passing tests |
| `event_engine` prefilter sweep | `tests/test_event_engine_prefilter.py` | passing tests |
| `event_engine` example runnable | synthetic example | manual smoke |

## Aggregate

- **Total tests at HEAD 62d178b:** 2,185 passing, 32 warnings.
- **Failed tests:** 0.
- **Doctor --burn-in:** 8/8 PASS, 0 WARN, 0 FAIL.
- **Pinned burner:** PID 89523, snapshot `.burnin_pin/62d178b...`.
- **Pre-market:** polls until `2026-07-30 08:30 CDT`.
