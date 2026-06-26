# Version Cycle Log

One-liner per change. Date, category, summary.

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
- **rl-rd** — Trained single-symbol AAPL PPO model (150K timesteps, 2023–2025) → OOS walk-forward: 17 trades, 35% WR, -$427 (best in 2/5 windows). Wider 5%/8% stops made it worse (11 trades, 27% WR, -$769) — entry timing is the bottleneck, not exits.
- **rl-config** — Made stop_loss_pct and profit_target_pct configurable in RLBacktestConfig (was hardcoded 3%/3%); set 5%/8% as run_rl_backtest defaults.
