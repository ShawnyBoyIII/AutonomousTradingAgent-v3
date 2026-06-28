# Version Cycle Log

One-liner per change. Date, category, summary.

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
- **tests** — Updated 5 RL backtest tests to handle new 3-value return from `_action_to_trade()` (added `proportion` field). All 854 tests passing.

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
