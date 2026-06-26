#!/usr/bin/env python3
"""Hyperparameter sweep for RL training — AAPL multi-year baseline."""
import sys
from pathlib import Path

from trading_bot.rl.agent import RLAgent, RLAgentConfig
from trading_bot.rl.env import TradingConfig
from trading_bot.rl.trainer import TrainingConfig

SWEEPS = [
    {"tag": "ent_coef_0.01", "ent_coef": 0.01},
    {"tag": "ent_coef_0.10", "ent_coef": 0.10},
]

SYMBOLS = ["AAPL"]
TIMESTEPS = 50_000
OUTPUT_DIR = Path("state/rl_logs/aapl_sweep")

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def train_one(sweep: dict, job_index: int) -> int:
    tag = sweep["tag"]
    msg = f"\n[Job {job_index}] Training AAPL with {tag}..."
    print(msg, flush=True)

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
    )

    training_config = TrainingConfig(
        env_config=env_config,
        model_type="PPO",
        total_timesteps=TIMESTEPS,
        learning_rate=sweep.get("learning_rate", 3e-4),
        n_epochs=10,
        batch_size=64,
        n_steps=128,
        gamma=sweep.get("gamma", 0.995),
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=sweep.get("ent_coef", 0.05),
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=0,
        log_dir=str(OUTPUT_DIR),
        eval_freq=5000,
        checkpoint_freq=10000,
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

    import json
    meta = {
        "symbols": SYMBOLS,
        "agent": "PPO",
        "tag": tag,
        **{k: v for k, v in sweep.items() if k != "tag"},
    }
    meta_path = OUTPUT_DIR / f"PPO_{tag}_meta.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    print(f"[Job {job_index}] Saved {tag}")
    return 0


def main() -> int:
    print(f"Sweep: {len(SWEEPS)} runs, {TIMESTEPS:,} timesteps each, symbols={SYMBOLS}")
    for i, sweep in enumerate(SWEEPS, 1):
        try:
            train_one(sweep, i)
        except Exception as exc:
            print(f"[Job {i}] FAILED: {exc}")
            import traceback
            traceback.print_exc()
    print("\nSweep complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
