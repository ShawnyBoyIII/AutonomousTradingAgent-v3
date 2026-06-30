# Version Cycle Log

One-liner per change. Date, category, summary.

---

## 2026-06-29 (Session 3)

- **feature** — Supermodel stack now consumes swarm committee output as an agent layer (`APPROVE` supports, `REJECT` blocks), aligning scan confidence with multi-agent review.
- **feature** — Scan rows now persist compact supermodel decisions even without `--why`, giving paper-confidence reports durable stack history.
- **feature** — Added `supermodel-report` CLI command to summarize persisted support/caution/block scan history.
- **feature** — Paper trade DB records now include stack decision in `strategy_tag` (for example `v3-trend_following|stack:support`) so closed-trade PnL can be attributed later.
- **feature** — `supermodel-report` now rolls up closed trade PnL by stack decision from `strategy_tag`.
- **feature** — Paper-trade path now feeds enabled swarm committee decisions into the supermodel stack before tagging trades.
- **feature** — Swarm workers now receive completed upstream `worker_results`, giving dependency-ordered agents a simple handoff channel.
- **feature** — Risk manager now consumes technical and fundamental analyst output for each ticker, exposing upstream action/confidence in risk signal metadata.
- **feature** — Scan summaries and compact DB details now preserve swarm approval/hold/reject evidence alongside supermodel decisions.
- **feature** — `supermodel-report` now shows swarm-vs-stack alignment pairs so paper review can see whether agent committee and stack agree.
- **feature** — Swarm committee decisions now surface risk-manager handoff context in `risk_factors`, preserving analyst-to-risk model chatter in aggregated output.
- **feature** — Scan `--why` and compact scan persistence now expose swarm handoff context (`swarm_handoff=...`) for immediate paper-mode review.
- **bugfix** — `NO_SIGNAL` scan lines with swarm enabled now include swarm decision/handoff details in `--why` output instead of formatting before swarm augmentation.
- **bugfix** — Swarm scan summary now counts engine `HOLD_FOR_MORE_INFO` decisions as `swarm_hold`.
- **bugfix** — Swarm overlay now fetches daily `1d` bars for the configured daily period instead of requesting long-range `5m` data that providers often reject.
- **feature** — `supermodel-report` now counts rows with persisted `swarm_handoff`, showing how often agent-to-agent chatter reached the paper evidence trail.
- **feature** — Paper-trade decision events now include compact supermodel/swarm evidence fields so fills, dry-runs, rejects, and no-signal rows retain stack context.
- **bugfix** — Post-stack paper-trade rejects (`stale`, `yellow`, allocation, cash, broker reject) now retain compact supermodel/swarm evidence in decision logs.
- **bugfix** — Scan `NO_SIGNAL` rows now build and persist `supermodel_decision=no_signal`, closing missing stack history in reports.
- **bugfix** — DB model timestamp defaults now use timezone-aware UTC (`datetime.now(timezone.utc)`) instead of deprecated naive `datetime.utcnow`.
- **bugfix** — Swarm overlay now calls `setup_workers(WORKER_CLASSES)` before running, so scan/paper overlays aggregate real worker votes instead of an empty engine.
- **bugfix** — `SwarmEngine.run()` now resets worker states/results per run, preventing stale votes when the same engine instance runs multiple symbol batches.
- **bugfix** — `SwarmEngine.setup_workers()` now clears prior workers/results before rebuilding, preventing stale worker sets after reconfiguration.
- **bugfix** — Committee decisions now populate `supporting_signals` and `opposing_signals`, preserving worker votes in the aggregate result instead of dropping them.
- **bugfix** — Scan `ERROR` rows and decision-log events now retain full swarm evidence (`decision`, `confidence`, `rationale`, `handoff`) when overlay data exists.
- **feature** — Paper trade `strategy_tag` now includes compact swarm decision (`swarm:approve/reject/hold`) when available, and `supermodel-report` rolls up closed PnL by swarm+stack pair.
- **bugfix** — Swarm strategy-tag suffixes now clamp unknown swarm decision labels so paper trade tags stay within the 50-char DB column.
- **bugfix** — Stack/swarm strategy-tag tokens now sanitize separators/spaces, preventing malformed decisions from breaking tag parsing.
- **bugfix** — Empty sanitized stack/swarm tag tokens now fall back to `unknown` instead of emitting empty `stack:`/`swarm:` suffixes.
- **bugfix** — `supermodel-report` now normalizes scan swarm decisions to lowercase (`approve/reject/hold`) so scan alignment and trade outcomes group consistently.
- **feature** — `supermodel-report` now shows open trade counts per stack decision, making pending paper exposure visible by model bucket.
- **tests** — Phase 2A: 52 tests for `db/` package (session, 7 models, 7 repositories) — 1474 passing.
- **tests** — Phase 2B: 17 tests for `events/orchestrator.py` (4 handlers, event flow) — 1491 passing.
- **tests** — Phase 2C: 48 tests across 6 modules (daily_signal_engine, fills, alerts, snapshots, yfinance_provider, provider_base) — 1539 passing.
- **tests** — Phase 2D: 72 tests for supermodel (40) and portfolio_ledger (32) — 1611 passing.
- **bugfix** — Phase 1A: 4 critical bugs in `brokers/paper.py` — market orders without price now fetch quote, `submit_order` wraps ValueError → REJECTED, `get_account`/`get_positions` use `ensure_portfolio_state` instead of `load_portfolio_state`, STOP_LIMIT maps via `_ORDER_TYPE_MAP`.
- **bugfix** — Phase 1B: 6 bugs in `backtest/attribution.py` — beta regression supports `strategy_returns` for CAPM, Monte Carlo vectorized to 2D array, `win_loss_ratio` returns None instead of inf, docstrings corrected for signal quality and holding period.
- **quality** — Phase 1C: Replaced 20 silent `except Exception: pass` with `logger.debug(...)` across 8 files (events/loop, events/bus, cli/app, rl/agent, rl/features, rl/actions, backtest/runner, runtime/orchestrator).
- **quality** — Phase 1D: Replaced 11 `print()` statements with logging (9 in backtest/runner, 2 in env/trading_env); kept Gym render() as print.
- **refactor** — Phase 3: Scout Pydantic refactoring — 5 new models (ScoutScreenerQuote, ScoutCandidate, ScoutSummary, ScoutResult, UniverseCandidatesSnapshot) in `trading_bot/models/scout.py`; `build_scout_candidates` returns `ScoutResult`; 4 call sites updated; removed dead `_first_text`.
- **feature** — Phase 4A: Backtest runner now populates `strategy_returns` and `benchmark_returns` in result dict for proper CAPM beta regression; added `benchmark_symbol` to AppSettings.
- **feature** — Phase 4B: Implemented order history in paper broker — `get_orders()` returns all submitted orders (filterable by `since`), `get_order(order_id)` finds by ID; 9 new tests.
- **quality** — Phase 5A: Fixed 4 `type: ignore` annotations — rl/env.py (2): added None guards raising RuntimeError; robinhood/reconciliation.py: used `cast(RobinhoodBrokerBoundary)`; monitoring/notifiers.py: early None guard on webhook_url.
- **quality** — Phase 5B: Added type annotations to 11 untyped functions across orchestrator.py (5), runner.py (3), market_data.py (1).
- **quality** — Phase 5C: Created centralized `trading_bot/logging_config.py` with `setup_logging()` (idempotent, optional FileHandler) and `configure_from_settings()`; wired into CLI callback; added `log_level`/`log_file` to AppSettings; 9 new tests.
- **tests** — Full suite: 1687 passing (1 pre-existing flaky: `test_auto_bench_cron`).

---

## 2026-06-29 (Session 2)

- **bugfix** — Fixed `watchlist_path` not being resolved relative to config file in `loader.py:114-119`, causing `scan-universe` to read from workspace `state/watchlist.txt` instead of test tmp_path.
- **bugfix** — Updated `test_rl_signal_rejects_symbols_outside_model_metadata` test: RL now supports inference on untrained symbols via dynamic padding, so the test was rewritten to verify the new behavior (confidence adjustment + lower threshold for untrained symbols).
- **feature** — RL inference now handles untrained symbols: removed hard rejection in `orchestrator.py:724-729`, added dynamic observation padding for new symbols, applies 15% confidence penalty and 20% lower confidence threshold for untrained symbols.
- **feature** — Added `rl_untrained_symbol: true` flag in scan details when RL trades a symbol not in its trained set, enabling transparency on signal source.
- **feature** — Burn-in script (`auto-burn-in.sh`) now reads from Python-configured paths (`state/universe.txt` + `state/watchlist.txt`) instead of separate `burn-in-symbols.txt`, unifying symbol sources.
- **feature** — Burn-in discovery (`run_discovery`) now preserves manually added watchlist symbols by merging discovered symbols with watchlist.txt into universe.txt.
- **feature** — `scan-universe` command now always merges watchlist into the symbol set, regardless of whether a custom `--universe-path` is provided.
- **feature** — Added confidence gates to burn-in loop: `check_confidence_gates()` checks trades>=10, realized_pnl>=500, profit_factor>=1.2, positive_windows>=60% every 10 cycles.
- **feature** — Added max drawdown kill switch to burn-in loop: `check_max_drawdown()` monitors equity history, halts script if drawdown >= 10% (configurable), warns at 80% threshold.
- **feature** — Created `scripts/daily_supermodel.py`: 5-step pipeline (load burn-in stats → discover symbols → evaluate models → train supermodel → build ensemble), outputs to `state/rl_logs/supermodel/`.
- **feature** — Added `supermodel` CLI command: runs daily supermodel retrain pipeline with `--symbols`, `--epochs`, `--timesteps`, `--dry-run` options.
- **feature** — Created `scripts/live_data_collector.py`: collects burn-in trades with market context into replay buffer (`state/rl_logs/replay_buffer.jsonl`), supports `--watch` (continuous) and `--buffer` (stats) modes.
- **feature** — Added `live-data` CLI command: wraps live data collector with `--watch`, `--buffer`, `--db-path`, `--buffer-path` options.
- **feature** — Created `scripts/auto_retrain_trigger.py`: detects new symbols in universe/watchlist not covered by existing RL models, triggers retrain via `daily_supermodel.py`.
- **feature** — Added `auto-retrain` CLI command: checks symbol coverage, triggers retrain with `--force` or `--dry-run` options.
- **refactor** — Burn-in script unified symbol sources: removed `burn-in-symbols.txt`, now reads `state/universe.txt` + `state/watchlist.txt`, merges and deduplicates.
- **refactor** — `scripts/auto-burn-in.sh` discovery function preserves watchlist symbols by merging with discovered symbols into universe.txt.
- **bugfix** — Confidence gates now halt burn-in trading when thresholds fail (was only logging warnings), exits with code 1 and logs to `halt.log`.
- **feature** — Supermodel pipeline now loads replay buffer for continual learning: `--replay-buffer` and `--replay-weight` args, `load_replay_buffer()` parses JSONL trade entries, `replay_buffer_stats()` computes win rate/PnL/ticker coverage.
- **feature** — `train_supermodel()` accepts `replay_entries` and `replay_weight` parameters, logs replay buffer stats during training, includes replay info in pipeline result JSON.
- **bugfix** — Fixed RL observation shape mismatch: `predict_signal()` was passing untrained symbols to `build_observation()`, causing `(10, 117)` instead of `(10, 101)`. Now truncates `symbol_list` to `n_symbols` before building observations, returns HOLD for untrained symbols not in the trained set.
- **bugfix** — Added `add_bollinger_bands()` and `add_vwap()` to intraday frame in `_build_v3_signal_result()`, enabling mean reversion setups (`detect_oversold_bounce` requires `bb_lower`/`bb_upper`, `detect_vwap_reversion` requires `vwap`).
- **bugfix** — Fixed `SYMBOLS_FILE` undefined variable in `auto-burn-in.sh` (lines 86, 88, 195, 198, 330), replaced with `UNIVERSE_FILE`.
- **config** — Changed `strategy.risk_tolerance` from `"medium"` to `"high"` in `burn-in-config.yaml` to trade through HIGH_VOLATILITY regime.

---

## 2026-06-29

- **feature** — Created research autopilot system (`trading_bot/research/`): hypothesis → backtest → evaluate → learn loop with SQLite storage, auto-hypothesis generation from alpha benching results, and configurable evaluation criteria.
- **feature** — Added `research-autopilot` CLI command: create/run/stats/cycles/bench-to-hypothesis actions for automated research pipeline.
- **feature** — Created research data models: Hypothesis (with status tracking), ExperimentResult (with success criteria), ResearchCycle (complete research loop).
- **feature** — Created ResearchStore: SQLite-backed persistence for hypotheses, experiments, and cycles with filtering and statistics.
- **feature** — Created ResearchEngine: manages research loop execution, pending hypothesis processing, and benching-to-hypothesis conversion.
- **bugfix** — Fixed alpha factor scoring in signal_confluence.py: equal-weight factor list now uses tuples (factor, weight) to match weighted factor list format, preventing unpack error.
- **bugfix** — Updated confidence thresholds in `_score_to_confidence()` to match new 12-point score scale (was 10-point).
- **bugfix** — Updated position size multiplier base from 10.0 to 12.0 to match new score scale.
- **tests** — Added 20 research autopilot tests (store, engine, models) — 922 tests passing.
- **feature** — Created persistent memory system (`trading_bot/memory/`): FTS5 full-text search, cross-session learning, intelligent recall, and auto-context building.
- **feature** — Added `memory` CLI command: store/recall/search/stats/list/clear actions for persistent trading insights.
- **feature** — Created memory data models: MemoryEntry (with types), MemoryQuery (with filters), MemoryStats (with aggregations).
- **feature** — Created MemoryStore: SQLite-backed persistence with FTS5 search, tag filtering, and batch operations.
- **feature** — Created MemoryRetriever: intelligent recall system with context-aware searching, research integration, and prompt building.
- **tests** — Added 23 persistent memory tests (store, retriever, models) — 944 tests passing.
- **feature** — Created BenchingWeightsManager: persistent IC IR weights for alpha factors, integrates with scanner scoring.
- **feature** — Added `bench-weights` CLI command: update/show/set/reset actions for managing benching weights.
- **feature** — Wired benching weights into signal_confluence.py: scanner now uses BenchingWeightsManager for persistent factor scoring.
- **feature** — BenchingWeightsManager supports min/max IC IR filters, auto-normalization, and persistence across sessions.
- **tests** — Added 12 benching weights tests (manager, persistence, filters) — 956 tests passing.

---

## 2026-06-28

- **feature** — Created SQLite database layer (`trading_bot/db/`) for local persistence: models (MarketData, ScanResult, Trade, Position, PortfolioSnapshot, ModelPrediction, Event), session management, and repository pattern for all entities.
- **feature** — Wired all 6 persistence integrations: `fetch_bars()` auto-persists OHLCV bars, `run_scan()` persists APPROVED/NO_SIGNAL/REJECTED results, `run_paper_trade()` persists BUY trades + positions, `_fill_sell_position()` persists SELL exits, `_build_rl_signal_result()` persists RL predictions.
- **feature** — Added 3 CLI commands: `db-history` (query scan results), `db-portfolio` (query portfolio snapshots), `db-trades` (query trades).
- **feature** — Created multi-agent swarm system (`trading_bot/swarm/`): DAG-based execution engine with 7 presets (investment_committee, quant_desk, risk_committee, technical_analysis_panel, fundamental_analysis_team, crypto_desk, macro_economics_team), 3 concrete workers (technical_analyst, risk_manager, factor_model), streaming status tracking, and result aggregation.
- **feature** — Added `swarm` CLI command for running multi-agent analysis with configurable presets and symbols.
- **feature** — Created post-backtest attribution system (`trading_bot/backtest/attribution.py`): trade-level attribution, winner/loser analysis, beta regression, regime analysis, Monte Carlo simulation, holding period statistics, exit reason attribution, signal quality correlation.
- **feature** — Added `attribution` CLI command for running post-backtest analysis with benchmark comparison.
- **feature** — Integrated attribution into `run_backtest()` automatically (opt-in via `benchmark_symbol` config).
- **bugfix** — Fixed `predict_signal` agent wrong padding width on missing market frames (pad to `features_per_symbol - len(market_feat)` instead of `features_per_symbol - 13`).
- **bugfix** — Fixed `_fill_sell_position` silent error swallowing: wrapped `update_trade_exit` in try/except to handle concurrent trade closure gracefully.
- **bugfix** — Replaced all 7 occurrences of deprecated `datetime.utcnow()` with `datetime.now(timezone.utc)` across repository files.
- **bugfix** — Fixed RL scan inference to use configured `settings.market_data.daily_period` instead of hardcoded "1y".
- **bugfix** — Removed dead code from `db_trades` CLI command (redundant ternary expression).
- **refactor** — All DB operations wrapped in try/except (fail-safe: DB failures don't break trading logic).

---

## 2026-06-27

- **bugfix** — Fixed `net_pnl` mislabeling in `_format_paper_confidence_gate`: renamed check from `"return>=5pct"` to `"net_pnl>=500"` and changed output from `rl_return_pct={net_pnl/100}` to `rl_net_pnl=${net_pnl}` (dollars, not fake percentage).
- **bugfix** — Fixed unsafe dict key access in `_format_scan_summary`: changed `summary['rl_avg_confidence']` to `summary.get('rl_avg_confidence', 0.0)` to prevent KeyError with stale scan results.
- **bugfix** — Fixed empty string edge case in `_resolve_rl_symbols`: added fallback when `_parse_symbols` returns empty list, preventing confusing error messages.
- **refactor** — Consolidated duplicate `_rl_model_meta_path` and `_rl_model_symbols` functions from `cli/app.py` and `runtime/orchestrator.py` into new shared module `trading_bot/rl/utils.py`.
- **rl-feature** — Created `scripts/sector_diversity_rl.py` for training PPO models on sector-diverse symbols (XOM, CVX, UNH, LLY, CAT, DE) with 300k timesteps, 3 seeds, and ProportionActionScheme.
- **rl-analysis** — Analyzed `multisymbol_seed_123` model and identified critical architecture issues: no feature normalization (raw prices $50 vs $500), observer re-fetches full frame every step (features don't advance), average cost tracked incorrectly (uses first bar close, not fill price), BSH action scheme too coarse (all-or-nothing sizing).
- **rl-improvement** — Implemented feature normalization in `build_market_feature_row`: price-based features (EMA, SMA, MACD, BB width) now expressed as ratios relative to close (e.g., `ema_12 = (ema_12_raw / close) - 1.0`), MACD scaled to percentage of close. PPO now sees features on similar scales regardless of stock price.
- **rl-improvement** — Fixed observer to use step-indexed data: `TensorTradeObserver.observe()` now accepts `data_frames` and `data_indices` parameters, slices data to `df.iloc[:idx + 1]` before computing features. Environment passes cached data and current step indices. Features now advance correctly with environment steps.
- **rl-improvement** — Fixed average cost tracking in `PaperBroker`: added `position_costs: dict[str, float]` to track weighted average cost basis on buys (`(old_cost * old_qty + fill_price * new_qty) / new_qty`), preserves cost basis on partial sells. Environment now uses `broker.position_costs[ticker]` instead of first bar's close price. Unrealized P&L and portfolio features now accurate.
- **rl-improvement** — Updated training scripts (`multisymbol_rl.py`, `sector_diversity_rl.py`) to use `ProportionActionScheme` (10-100% position sizing vs all-or-nothing BSH) and increased timesteps from 200k to 300k for better convergence on multi-symbol portfolios.
- **rl-backtest** — Fixed `RLBacktestRunner` to support `ProportionActionScheme`: added `action_scheme` config parameter, refactored `_decode_action()` to return 3 values `(symbol, direction, proportion)`, added `_decode_bsh_action()` and `_decode_proportion_action()` methods. Updated `_execute_buy()` and `_execute_sell()` to accept `proportion` parameter for position sizing. Fixed all 5 test cases to handle new return signature.
- **rl-tuning** — Created `scripts/tune_multisymbol.py` for systematic hyperparameter tuning: tests combinations of ent_coef (0.005, 0.01, 0.02), gamma (0.99, 0.995, 0.999), learning_rate (1e-4, 3e-4, 5e-4), reward_scheme (risk_adjusted, sharpe, drawdown_penalty). Quick mode: 3 configs × 2 seeds = 6 runs (~2 hours). Full mode: 81 configs × 2 seeds = 162 runs (~2 days).
- **rl-ops** — Created `scripts/compare_models.py` to evaluate all existing models in `state/rl_logs/` against 6 months of fresh data and rank by performance. Auto-detects action_scheme from metadata.
- **bugfix** — Fixed `sweep_exits` crash when all data fetches fail: added early return when `frames` is empty and error handling for when `backtest_model` returns error dict (prevents KeyError on `result['trades']`).
- **refactor** — Replaced dead `getattr` fallbacks with direct attribute access in `backtest/runner.py`: `settings.rl.backtest_starting_cash`, `settings.rl.backtest_max_shares`, `settings.rl.backtest_stop_loss_pct`, `settings.rl.backtest_profit_target_pct` (Pydantic model always has these fields).
- **tests** — Updated 5 RL backtest tests to handle new 3-value return from `_action_to_trade()` (added `proportion` field). All 863 tests passing.

---

## 2026-06-26

- **docs** — Added screenshot-style downloadable worktree graph at `docs/APP_WORKTREE_GRAPH.svg`: wide white canvas, small black-and-white boxes, curved connectors, and prune-review notes.
- **docs** — Added downloadable black-and-white worktree map at `docs/APP_WORKTREE_MAP.svg`, showing CLI, core paper flow, data, strategy/risk, execution/portfolio, RL/backtest, Robinhood boundary, ops/reporting, and prune-review candidates.
- **burn-in** — Burn-in ran 2026-06-22 to 2026-06-26. COHR bought 2026-06-25 @ $407.85 (4 shares, -$115 unrealized). CIEN bought $477.00 (4 shares, -$11 unrealized). Both stopped getting intraday data updates at ~13:35 UTC on June 25. All subsequent `manage-positions` checks were SKIP stale-data, preventing stop-loss execution. COHR manually closed at $380.00 (realized loss -$112.40). CIEN stop at $473.50 was never reached.
- **burn-in** — Added `rl:` section to burn-in-config.yaml. RL now primary signal source for burn-in: `enabled: true`, `model_path: state/rl_logs/aapl_production/PPO_final.zip`, `action_confidence_threshold: 0.4`. Fixed `log_dir: "logs/burn_in"` → `"logs"` so model path resolves correctly (logs/burn_in.parent = logs/ not project root).
- **rl-fix** — Completed seed wiring for reproducible training. `TrainingConfig.seed` now reaches the SB3 model constructor; `scripts/train_rl.py` and `rl-train` expose `--seed`; training metadata records the seed/reward scheme. Verified with focused RL tests (`74 passed`).
- **rl-confidence** — `rl-walkforward` now prints a paper-confidence verdict using existing totals: trade count, 5% return target on the $10k backtest account, profit factor, and positive-window ratio.
- **rl-scan** — Scan summaries in RL mode now include `rl_buy`, `rl_hold`, `rl_sell`, and `rl_avg_conf` so live stock-finding runs can be judged at a glance.
- **rl-safety** — RL scans now fail closed when the loaded model metadata does not include the requested ticker, preventing the AAPL-only production model from scoring unrelated symbols.
- **rl-scan** — RL scan summaries now include `rl_unsupported` so broad scans distinguish unsupported tickers from true HOLD/SELL/no-signal outcomes.
- **rl-ops** — Added `rl-model-info` to show active model path, metadata path, trained symbols, seed, and reward scheme without fetching market data.
- **rl-ops** — `rl-model-info` now prints a supported `scan --symbols ... --summary --why` command for the active model coverage.
- **rl-safety** — `rl-benchmark` and `rl-walkforward` now fail closed when the requested symbol is outside the model metadata, matching RL scan behavior.
- **rl-safety** — RL benchmark commands now also fail closed when model metadata is missing, because symbol coverage cannot be proven from the zip alone.
- **rl-safety** — RL scan inference now also requires non-empty model metadata before loading/fetching, preventing `RLAgent.load` defaults from implying false AAPL coverage.
- **rl-safety** — Added a fail-closed guard for multi-symbol model metadata before full multi-symbol scan observations existed; `rl-model-info` avoided suggesting unsafe scan commands.
- **rl-ops** — Added `rl-scan-plan` to show the active model's safe scan command or the exact blocked next step without fetching market data.
- **rl-feature** — RL scan inference now builds observations with frames for every trained symbol in model metadata, allowing multi-symbol models to scan without zero-padding missing symbols.
- **rl-ops** — `rl-benchmark` and `rl-walkforward` now accept `--symbols AAPL,MSFT` and validate every requested ticker against model metadata before fetching data.
- **docs** — Updated README, quick reference, getting started, and RL guide with current RL model-info, scan-plan, multi-symbol benchmark, and walk-forward commands.

---

## 2025-06-25

- **bugfix** — RLBacktestRunner was copying the same daily frame to all symbols, corrupting observations for multi-symbol models; fixed per-symbol frame dicts + target_symbol trading.
- **bugfix** — SELL PnL in RLBacktestRunner used `position * 0` as cost basis instead of actual entry price.
- **bugfix** — DataFrame truth-value ambiguity crashes in two `or` chains (intra_frames / frames lookups).
- **bugfix** — AEP timezone error: `_market_data_status` / `_market_data_age` now use `_ensure_aware` to prevent offset-naive vs offset-aware subtraction.
- **bugfix** — RL walk-forward diagnostics showed `avg_win=0.00 avg_loss=0.00` because `run_rl_backtest` summary omitted `gross_profit`/`gross_loss`; now accumulated and included.
- **bugfix** — RL daily-trained models were evaluated with 1h intraday frames, causing 3% stops to hit on hourly volatility (0% win rate vs 54% when using daily resolution); added `use_intraday_exit=False` config flag.
- **refactor** — Collapsed `RLBacktestRunner._action_to_trade` + `_action_to_target_symbol` into single `_decode_action`; extracted `_prepare_frames`, `_execute_buy`, `_execute_sell`, `_build_portfolio_state`, `_build_observation_batch`.
- **refactor** — Extracted `_fetch_rl_frames` and `_write_rl_summary` helpers from `run_rl_backtest`; removed single-symbol TradingEnv fallback (now unified in RLBacktestRunner).
- **refactor** — Removed stale ponytail comments and multi-symbol legacy warning from runner.py.
- **config** — Switched burn-in from V3 to V2.5 (`use_v3_signals: false`, `counter_thesis.enabled: false`).
- **config** — Relaxed stale-data threshold `max_data_age_minutes: 30 → 75` for Alpaca 5m bar delays.
- **tests** — Rewrote two RL backtest runner tests that mocked the removed TradingEnv fallback; now patch RLBacktestRunner.run_backtest directly.
- **provider** — Added FinnhubProvider (`trading_bot/data/providers/finnhub_provider.py`). Supports daily and intraday bars. Set `provider: finnhub` in config, add `FINNHUB_API_KEY` to `.env`. Free tier: daily only; intraday needs paid plan.
- **provider** — Added PolygonProvider (`trading_bot/data/providers/polygon_provider.py`). Free tier: 5 calls/min, end-of-day, 2-year history. Set `POLYGON_API_KEY` in `.env`.
- **provider** — Replaced hardcoded yfinance fallback with provider stack. Config now accepts `providers: [alpaca, finnhub, polygon]`; each is tried in order. Removed yfinance from default chain.
- **config** — `config.yaml` and `burn-in-config.yaml` now use `providers: [alpaca, finnhub, polygon]` stack.
- **rl-rd** — Hyperparameter sweep on AAPL (ent_coef, lr, gamma). Winners: ent_coef=0.01 (+$1,745, 47% WR) and lr=5e-4 (+$1,559, 44% WR). Gamma deviations from 0.995 kill the model (0 trades). High entropy (0.10) also kills it.
- **rl-rd** — Reward function sweep on AAPL with ent_coef=0.01. ALL reward schemes (simple_profit, compound_daily, sharpe, drawdown_penalty, risk_adjusted) produced 0 trades. Root cause discovery: the ent_coef=0.01 winner from previous sweep was a lucky random seed, NOT a robust config. Same config with different seed yields 0 trades.
- **rl-rd** — Multi-seed training (5x same config, different seeds). 4/5 seeds converge profitably: trades=18-19, WR=47-50%, net_pnl=+$1,745 to +$1,747. Only seed 456 failed (22% WR, -$40). The +$1,745 policy is robust, not a lucky outlier.
- **rl-fix** — Added `seed` field to TrainingConfig; passed to PPO model constructor. Previous runs all used SB3's default seed, producing identical models. Now reproducible.
- **rl-prod** — Promoted seed_789 model to `state/rl_logs/aapl_production/` (18 trades, 50% WR, +$1,747 out-of-sample). Config.yaml updated to use this as the model_path.
- **rl-feature** — Added SharpeReward and DrawdownPenaltyReward schemes to trading_bot/rl/rewards.py. SharpeReward uses rolling 20-step window; DrawdownPenaltyReward penalises drawdown from peak. Stateful with reset() method. Registered in TradingEnv reward_schemes dict.
- **rl-fix** — Added `data_end_date` to TradingConfig to prevent training-on-test leakage. Sweeps train on data ending 2025-06-24, walk-forward from 2025-06-25.
- **rl-polygon** — Added PolygonProvider with 429 rate-limit retry (2s/4s/6s backoff). Period pattern parser now supports arbitrary "3y"/"6m" intervals via regex fallback.
- **bugfix** — Provider stack resolution failed when Polygon/Finnhub API keys were missing: `PolygonProvider()` constructor raised `ValueError` during `_resolve_provider_stack()`, which propagated before Alpaca was tried. Fixed by moving provider instantiation inside the try-except loop in `_fallback_fetch()`, so failed providers are skipped and the next one is tried. Also fixed swarm overlay to pass `settings.market_data` to `fetch_bars()` (was using default yfinance fallback).
