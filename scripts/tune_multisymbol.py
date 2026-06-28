#!/usr/bin/env python3
"""Systematic hyperparameter tuning for multi-symbol RL models.

Tests combinations of:
- ent_coef: 0.005, 0.01, 0.02
- gamma: 0.99, 0.995, 0.999
- learning_rate: 1e-4, 3e-4, 5e-4
- reward_scheme: risk_adjusted, sharpe, drawdown_penalty

Usage:
    .venv/bin/python scripts/tune_multisymbol.py --config quick
    .venv/bin/python scripts/tune_multisymbol.py --config full
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from itertools import product

from trading_bot.rl.agent import RLAgent, RLAgentConfig
from trading_bot.rl.env import TradingConfig
from trading_bot.rl.trainer import TrainingConfig
from trading_bot.rl.backtest import RLBacktestRunner, RLBacktestConfig

SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA"]
TIMESTEPS = 150_000
SEEDS = [42, 123]
OUTPUT_DIR = Path("state/rl_logs/tuning")
RESULTS_DIR = OUTPUT_DIR / "results"

QUICK_CONFIGS = [
    {"ent_coef": 0.01, "gamma": 0.995, "lr": 3e-4, "reward": "risk_adjusted"},
    {"ent_coef": 0.005, "gamma": 0.999, "lr": 1e-4, "reward": "sharpe"},
    {"ent_coef": 0.02, "gamma": 0.99, "lr": 5e-4, "reward": "drawdown_penalty"},
]

FULL_CONFIGS = [
    {"ent_coef": ec, "gamma": g, "lr": lr, "reward": r}
    for ec, g, lr, r in product(
        [0.005, 0.01, 0.02],
        [0.99, 0.995, 0.999],
        [1e-4, 3e-4, 5e-4],
        ["risk_adjusted", "sharpe", "drawdown_penalty"],
    )
]


def train_and_evaluate(config: dict, seed: int, run_id: str) -> dict:
    print(f"\n{'='*70}")
    print(f"  Run: {run_id} | Seed: {seed}")
    print(f"  Config: {config}")
    print(f"{'='*70}\n", flush=True)

    run_dir = OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    env_config = TradingConfig(
        symbols=SYMBOLS,
        bar_period="1y",
        bar_interval="1d",
        observer_window=10,
        starting_cash=100_000.0,
        fee_per_order=1.0,
        slippage_bps=5,
        max_positions=10,
        max_episode_steps=500,
        reward_scheme=config["reward"],
        action_scheme="proportion",
    )

    training_config = TrainingConfig(
        env_config=env_config,
        model_type="PPO",
        total_timesteps=TIMESTEPS,
        learning_rate=config["lr"],
        n_epochs=10,
        batch_size=64,
        n_steps=128,
        gamma=config["gamma"],
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=config["ent_coef"],
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=0,
        log_dir=str(run_dir),
        eval_freq=5000,
        checkpoint_freq=10000,
        seed=seed,
    )

    agent_config = RLAgentConfig(
        enabled=True,
        env_config=env_config,
        training=training_config,
        prediction_mode="deterministic",
    )

    agent = RLAgent(config=agent_config)

    try:
        agent.train()
    except Exception as e:
        print(f"  Training failed: {e}", flush=True)
        return {"run_id": run_id, "seed": seed, "config": config, "error": str(e)}

    model_path = run_dir / f"PPO_seed_{seed}"
    agent.save(model_path)

    meta = {
        "symbols": SYMBOLS,
        "agent": "PPO",
        "seed": seed,
        "ent_coef": config["ent_coef"],
        "gamma": config["gamma"],
        "learning_rate": config["lr"],
        "reward_scheme": config["reward"],
        "total_timesteps": TIMESTEPS,
        "action_scheme": "proportion",
        "trained_at": datetime.now().isoformat(),
    }
    meta_path = run_dir / f"PPO_seed_{seed}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(f"  Model saved: {model_path}.zip", flush=True)

    from trading_bot.data import market_data

    frames = {}
    for sym in SYMBOLS:
        df = market_data.fetch_bars(sym, period="6mo", interval="1d")
        if df is not None and not df.empty:
            frames[sym] = df

    if not frames:
        return {"run_id": run_id, "seed": seed, "config": config, "error": "no data"}

    bt_config = RLBacktestConfig(
        model_path=str(model_path.with_suffix(".zip")),
        symbols=list(frames.keys()),
        starting_cash=100_000.0,
        fee_per_order=1.0,
        slippage_bps=5,
        use_intraday_exit=False,
        stop_loss_pct=0.05,
        profit_target_pct=0.08,
        action_scheme="proportion",
    )

    runner = RLBacktestRunner(config=bt_config)
    runner.load_model()

    result = runner.run_backtest(
        daily_frames=frames,
        starting_cash=100_000.0,
        trade_symbols=list(frames.keys()),
    )

    result.update({
        "run_id": run_id,
        "seed": seed,
        "config": config,
        "backtested_at": datetime.now().isoformat(),
    })

    print(f"\n  Backtest Results:", flush=True)
    print(f"    Trades:   {result['trades']}", flush=True)
    print(f"    Win Rate: {result['win_rate']:.0%}", flush=True)
    print(f"    Net PnL:  ${result['net_pnl']:.2f}", flush=True)

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", choices=["quick", "full"], default="quick")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    configs = QUICK_CONFIGS if args.config == "quick" else FULL_CONFIGS
    print(f"Tuning mode: {args.config} ({len(configs)} configs × {len(SEEDS)} seeds = {len(configs) * len(SEEDS)} runs)")
    print(f"Symbols: {SYMBOLS}")
    print(f"Timesteps per run: {TIMESTEPS:,}\n", flush=True)

    all_results = []

    for i, config in enumerate(configs):
        run_id = f"run_{i:03d}_ec{config['ent_coef']}_g{config['gamma']}_lr{config['lr']}_r{config['reward']}"

        for seed in SEEDS:
            try:
                result = train_and_evaluate(config, seed, run_id)
                all_results.append(result)

                result_file = RESULTS_DIR / f"{run_id}_seed_{seed}.json"
                result_file.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

            except Exception as e:
                print(f"\n  FAILED: {e}", flush=True)
                import traceback
                traceback.print_exc()
                all_results.append({"run_id": run_id, "seed": seed, "config": config, "error": str(e)})

    summary_file = RESULTS_DIR / "tuning_summary.json"
    summary = {
        "symbols": SYMBOLS,
        "seeds": SEEDS,
        "timesteps": TIMESTEPS,
        "total_runs": len(all_results),
        "results": all_results,
        "generated_at": datetime.now().isoformat(),
    }
    summary_file.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"\n{'='*70}")
    print(f"  TUNING COMPLETE")
    print(f"{'='*70}")
    print(f"  Total runs: {len(all_results)}")
    print(f"  Results saved to: {RESULTS_DIR}")

    successful = [r for r in all_results if "error" not in r]
    if successful:
        best = max(successful, key=lambda r: r.get("net_pnl", -float("inf")))
        print(f"\n  Best config:")
        print(f"    Run ID:    {best['run_id']}")
        print(f"    Seed:      {best['seed']}")
        print(f"    Config:    {best['config']}")
        print(f"    Trades:    {best['trades']}")
        print(f"    Win Rate:  {best['win_rate']:.0%}")
        print(f"    Net PnL:   ${best['net_pnl']:.2f}")

    print(f"{'='*70}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
