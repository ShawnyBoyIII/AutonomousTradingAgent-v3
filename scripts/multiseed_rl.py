#!/usr/bin/env python3
"""Multi-seed training: same best config, different seeds, keep best model."""
import sys
import json
from pathlib import Path

from trading_bot.rl.agent import RLAgent, RLAgentConfig
from trading_bot.rl.env import TradingConfig
from trading_bot.rl.trainer import TrainingConfig

SEEDS = [42, 123, 456, 789, 1024]

SYMBOLS = ["AAPL"]
TIMESTEPS = 150_000
OUTPUT_DIR = Path("state/rl_logs/aapl_multiseed")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def train_one(seed: int) -> int:
    tag = f"seed_{seed}"
    print(f"\n[Seed {seed}] Training AAPL...", flush=True)

    env_config = TradingConfig(
        symbols=SYMBOLS,
        bar_period="1y",
        bar_interval="1d",
        data_end_date="2025-06-24",
        observer_window=10,
        starting_cash=100_000.0,
        fee_per_order=1.0,
        slippage_bps=5,
        max_positions=10,
        max_episode_steps=500,
        reward_scheme="risk_adjusted",
    )

    training_config = TrainingConfig(
        env_config=env_config,
        model_type="PPO",
        total_timesteps=TIMESTEPS,
        learning_rate=3e-4,
        n_epochs=10,
        batch_size=64,
        n_steps=128,
        gamma=0.995,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=0,
        log_dir=str(OUTPUT_DIR),
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
    agent.train()

    model_path = OUTPUT_DIR / f"PPO_{tag}"
    agent.save(model_path)

    meta = {"symbols": SYMBOLS, "agent": "PPO", "seed": seed, "ent_coef": 0.01}
    meta_path = OUTPUT_DIR / f"PPO_{tag}_meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    print(f"[Seed {seed}] Saved {tag}", flush=True)
    return 0


def main() -> int:
    print(f"Multi-seed: {len(SEEDS)} runs, {TIMESTEPS:,} timesteps each", flush=True)
    for seed in SEEDS:
        try:
            train_one(seed)
        except Exception as exc:
            print(f"[Seed {seed}] FAILED: {exc}", flush=True)
            import traceback
            traceback.print_exc()
    print("\nMulti-seed training complete.", flush=True)
    print("Models saved to:", OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())