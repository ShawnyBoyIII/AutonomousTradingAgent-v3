# RL Trading Guide

Small truth-first guide for the current RL path.

## What exists now

- Training CLI: `rl-train`
- Evaluation CLI: `rl-eval`
- Strategy benchmark: `rl-benchmark`
- Sequential benchmark: `rl-walkforward`
- Generic compare path: `backtest --compare`
- Supported agents today: `PPO`, `A2C`, `DQN`

Not current anymore:

- `SAC`, `TD3`, `DDPG`
- `--feature-set` on the RL CLI

## Install

```bash
./tradebot-local doctor
.venv/bin/pip install -e ".[rl]"
```

## Train a model

Single symbol:

```bash
./tradebot-local rl-train --symbols AAPL --agent PPO --timesteps 50000
```

Two symbols:

```bash
./tradebot-local rl-train --symbols AAPL,MSFT --agent PPO --timesteps 50000
```

Notes:

- Models save into `state/rl_logs/`
- Final model names look like `state/rl_logs/PPO_final.zip`
- The model shape depends on the training symbol set, so keep track of it

## Evaluate a trained model

Evaluation must use the same symbol set used during training.

```bash
./tradebot-local rl-eval \
  --symbols AAPL,MSFT \
  --train-symbols AAPL,MSFT \
  --agent PPO \
  --episodes 1
```

What `--train-symbols` means:

- it is not optional bookkeeping
- it tells the evaluator what environment shape the model was trained on
- if it does not match `--symbols`, evaluation fails closed

## Run the benchmark against V2.5 and V3

Use a config with RL enabled and a real model path:

```yaml
strategy:
  use_v3_signals: true

rl:
  enabled: true
  agent_type: PPO
  model_path: state/rl_logs/PPO_final.zip
```

Then run:

```bash
./tradebot-local rl-benchmark \
  --symbol AAPL \
  --start 2025-06-25 \
  --end 2026-06-25
```

Or run sequential windows:

```bash
./tradebot-local rl-walkforward \
  --symbol AAPL \
  --start 2025-06-25 \
  --end 2026-06-25 \
  --windows 5
```

Important:

- `rl-benchmark` is the apples-to-apples single-symbol path
- `rl-walkforward` repeats that comparison across sequential windows
- `rl-walkforward` does not retrain between windows yet; same saved model used in every window
- it always runs `v2.5`, `v3`, and `rl` on the same symbol and same date window
- the RL side now uses the same `TradingEnv` family as training for the single-symbol benchmark path
- it uses the configured `rl.model_path`, or `--model-path` if you override it

Reward scheme notes:

- supported env reward schemes today: `simple_profit`, `risk_adjusted`, `compound_daily`, `shannon_entropy`
- `risk_adjusted` is still the safest default for local training
- benchmark and eval now expose more honest episode stats internally, including trade count

Benchmark diagnostics:

- `avg_win`: average dollars gained on winning trades
- `avg_loss`: average dollars lost on losing trades
- `expectancy`: average dollars gained or lost per trade
- `profit_factor`: gross profit divided by absolute gross loss
- `pnl_per_trade`: same as expectancy, shown for scanability

If you still want the generic compare command:

```bash
./tradebot-local backtest \
  --symbols AAPL \
  --start 2025-06-25 \
  --end 2026-06-25 \
  --compare
```

## What to trust

- trust the command wiring and the benchmark path more than the current saved PPO quality
- trust single-symbol `rl-benchmark` over generic `backtest --compare` when you are checking RL against V2.5 and V3
- do not over-read one lucky RL equity number without checking trade count and a few date windows

## Should you train on more stocks?

Not first.

What to do first:

- get one-symbol training and one-symbol benchmarking trustworthy
- make RL at least competitive on `AAPL` alone
- repeat across a few date windows

Why not jump to more stocks yet:

- the model shape depends on the training symbol set
- adding symbols changes both observation size and action space
- if the single-symbol benchmark is still weak, multi-symbol training just makes debugging harder

When to add more stocks:

- after a single-symbol model is stable
- after you can show it is not just overfitting one lucky window
- start small: `AAPL,MSFT`, not a big basket

## Yahoo / date-range gotcha

If you use `yfinance`, long old ranges can miss 5-minute bars. The backtest path falls back when it can, but for the easiest smoke run use a recent window first.

If you want older ranges regularly, use a provider/config that can supply the intraday history you need.

## Quick troubleshooting

Model not found:

```bash
ls state/rl_logs
```

RL not showing up in compare:

- check `rl.enabled: true`
- check `rl.model_path` points to a real file

Evaluation rejected:

- make `--symbols` and `--train-symbols` match exactly

Training or benchmark fails on data fetch:

- check the market-data provider in your config
- for quick local smoke tests, `yfinance` is the easiest path
