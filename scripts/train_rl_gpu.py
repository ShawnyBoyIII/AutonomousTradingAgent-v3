#!/usr/bin/env python3
"""GPU-Optimized RL Training Script for Autonomous Trading Agent.

Trains PPO agents on TradingEnv with GPU acceleration via PyTorch/CUDA.
Designed for RTX 5070 / consumer GPUs.

Usage:
    # Train 3 models on universe symbols, 500K steps each
    python scripts/train_rl_gpu.py --symbols SOFI,NVDA,MED,GCTS,AVXL,EBS,PRTA --timesteps 500000 --num-seeds 3

    # Try different reward schemes
    python scripts/train_rl_gpu.py --symbols SOFI,NVDA,MED --timesteps 300000 --reward simple_profit --num-seeds 2

    # Evaluate after training
    python scripts/train_rl_gpu.py --symbols SOFI,NVDA,MED --evaluate --model-dir state/rl_logs/gpu_run_1

    # Train on full universe with daily data
    python scripts/train_rl_gpu.py --symbols SOFI,NVDA,MED,GCTS,AVXL,EBS,PRTA,AFL,BAC,BAX,CIEN,COHR,ILPT,NABL,UTZ,AEP,TNXP --timesteps 500000 --interval 1d --num-seeds 5

    # Quick test: 50K steps to verify GPU is working
    python scripts/train_rl_gpu.py --symbols SOFI --timesteps 50000 --num-seeds 1 --verbose 2
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GPU-optimized RL training for trading agent")
    parser.add_argument(
        "--symbols",
        type=str,
        default="SOFI,NVDA,MED",
        help="Comma-separated list of symbols to train on",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="PPO",
        choices=["PPO", "A2C"],
        help="DRL agent type (default: PPO)",
    )
    parser.add_argument(
        "--timesteps",
        type=int,
        default=500000,
        help="Total timesteps per model (default: 500K)",
    )
    parser.add_argument(
        "--num-seeds",
        type=int,
        default=3,
        help="Number of models to train with different seeds (default: 3)",
    )
    parser.add_argument(
        "--reward",
        type=str,
        default="risk_adjusted",
        choices=["simple_profit", "risk_adjusted", "sharpe", "drawdown_penalty", "compound_daily"],
        help="Reward scheme (default: risk_adjusted)",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=3e-4,
        help="Learning rate (default: 3e-4)",
    )
    parser.add_argument(
        "--interval",
        type=str,
        default="1d",
        choices=["1d", "5m", "15m"],
        help="Data interval for training (default: 1d)",
    )
    parser.add_argument(
        "--period",
        type=str,
        default="1y",
        help="Data period (default: 1y)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Output directory (default: state/rl_logs/gpu_run_<timestamp>)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Base random seed (default: random)",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Evaluate trained models instead of training",
    )
    parser.add_argument(
        "--eval-episodes",
        type=int,
        default=20,
        help="Number of evaluation episodes per model (default: 20)",
    )
    parser.add_argument(
        "--model-dir",
        type=str,
        default=None,
        help="Directory containing models to evaluate",
    )
    parser.add_argument(
        "--n-epochs",
        type=int,
        default=10,
        help="PPO n_epochs (default: 10)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="PPO batch_size (default: 256, larger for GPU)",
    )
    parser.add_argument(
        "--n-steps",
        type=int,
        default=512,
        help="PPO n_steps (default: 512, larger for GPU)",
    )
    parser.add_argument(
        "--gamma",
        type=float,
        default=0.995,
        help="Discount factor (default: 0.995)",
    )
    parser.add_argument(
        "--gae-lambda",
        type=float,
        default=0.95,
        help="GAE lambda (default: 0.95)",
    )
    parser.add_argument(
        "--clip-range",
        type=float,
        default=0.2,
        help="PPO clip range (default: 0.2)",
    )
    parser.add_argument(
        "--ent-coef",
        type=float,
        default=0.02,
        help="Entropy coefficient (default: 0.02)",
    )
    parser.add_argument(
        "--vf-coef",
        type=float,
        default=0.5,
        help="Value function coefficient (default: 0.5)",
    )
    parser.add_argument(
        "--max-grad-norm",
        type=float,
        default=0.5,
        help="Max gradient norm (default: 0.5)",
    )
    parser.add_argument(
        "--reward-scale",
        type=float,
        default=100.0,
        help="Reward scale factor (default: 100.0)",
    )
    parser.add_argument(
        "--observer-window",
        type=int,
        default=10,
        help="Observer window size (default: 10)",
    )
    parser.add_argument(
        "--max-episode-steps",
        type=int,
        default=500,
        help="Max steps per episode (default: 500)",
    )
    parser.add_argument(
        "--verbose",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Verbosity level (default: 1)",
    )
    return parser.parse_args()


def check_gpu() -> dict[str, Any]:
    """Check GPU availability and return info."""
    try:
        import torch
        has_gpu = torch.cuda.is_available()
        info = {
            "available": has_gpu,
            "device": "cuda" if has_gpu else "cpu",
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda if has_gpu else None,
            "gpu_name": torch.cuda.get_device_name(0) if has_gpu else None,
            "gpu_memory": torch.cuda.get_device_properties(0).total_memory / 1e9 if has_gpu else None,
            "num_gpus": torch.cuda.device_count() if has_gpu else 0,
        }
        return info
    except ImportError:
        return {
            "available": False,
            "device": "cpu",
            "torch_version": "not installed",
            "cuda_version": None,
            "gpu_name": None,
            "gpu_memory": None,
            "num_gpus": 0,
        }


def train_single_model(
    symbol_list: list[str],
    seed: int,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, Any]:
    """Train a single model with given seed."""
    from trading_bot.rl.agent import RLAgent, RLAgentConfig
    from trading_bot.rl.env import TradingConfig
    from trading_bot.rl.trainer import TrainingConfig as RLTrainingConfig

    model_dir = output_dir / f"GPU_seed_{seed}"
    model_dir.mkdir(parents=True, exist_ok=True)

    env_config = TradingConfig(
        symbols=symbol_list,
        bar_period=args.period,
        bar_interval=args.interval,
        observer_window=args.observer_window,
        starting_cash=100_000.0,
        fee_per_order=1.0,
        slippage_bps=5,
        max_positions=10,
        max_episode_steps=args.max_episode_steps,
        reward_scheme=args.reward,
        reward_scale=args.reward_scale,
    )

    training_config = RLTrainingConfig(
        env_config=env_config,
        model_type=args.agent,
        total_timesteps=args.timesteps,
        learning_rate=args.learning_rate,
        n_epochs=args.n_epochs,
        batch_size=args.batch_size,
        n_steps=args.n_steps,
        gamma=args.gamma,
        gae_lambda=args.gae_lambda,
        clip_range=args.clip_range,
        ent_coef=args.ent_coef,
        vf_coef=args.vf_coef,
        max_grad_norm=args.max_grad_norm,
        verbose=args.verbose,
        seed=seed,
        log_dir=str(model_dir),
    )

    agent_config = RLAgentConfig(
        enabled=True,
        env_config=env_config,
        training=training_config,
        prediction_mode="deterministic",
    )

    agent = RLAgent(config=agent_config)

    start_time = time.time()
    logger.info(f"Training GPU_seed_{seed} on {', '.join(symbol_list)}...")
    logger.info(f"  Timesteps: {args.timesteps:,}")
    logger.info(f"  Reward: {args.reward}")
    logger.info(f"  Device: {check_gpu()['device']}")

    trainer = agent.train()

    model_path = model_dir / f"{args.agent}_final"
    agent.save(model_path)
    saved_model_path = model_path.with_suffix(".zip")

    elapsed = time.time() - start_time
    tps = args.timesteps / elapsed if elapsed > 0 else 0

    result = {
        "seed": seed,
        "model_path": str(saved_model_path),
        "elapsed_seconds": elapsed,
        "timesteps_per_second": tps,
        "symbols": symbol_list,
        "reward": args.reward,
        "device": check_gpu()["device"],
    }

    meta = {
        "symbols": symbol_list,
        "agent": args.agent,
        "seed": seed,
        "reward_scheme": args.reward,
        "timesteps": args.timesteps,
        "interval": args.interval,
        "period": args.period,
        "device": check_gpu()["device"],
        "training_time_seconds": elapsed,
        "timesteps_per_second": tps,
    }
    meta_path = model_dir / f"{args.agent}_final_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # Also save as training_meta.json for our own tracking
    training_meta_path = model_dir / "training_meta.json"
    training_meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    logger.info(f"  Model saved to: {saved_model_path}")
    logger.info(f"  Training time: {elapsed:.1f}s ({tps:.0f} timesteps/sec)")

    return result


def evaluate_model(
    model_path: Path,
    symbol_list: list[str],
    n_episodes: int,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Evaluate a single trained model."""
    from trading_bot.rl.agent import RLAgent, RLAgentConfig
    from trading_bot.rl.env import TradingConfig
    from trading_bot.rl.trainer import TrainingConfig as RLTrainingConfig

    env_config = TradingConfig(
        symbols=symbol_list,
        bar_period=args.period,
        bar_interval=args.interval,
        observer_window=args.observer_window,
        starting_cash=100_000.0,
        fee_per_order=1.0,
        slippage_bps=5,
        max_positions=10,
        max_episode_steps=args.max_episode_steps,
        reward_scheme=args.reward,
        reward_scale=args.reward_scale,
    )

    training_config = RLTrainingConfig(
        env_config=env_config,
        model_type=args.agent,
        total_timesteps=50000,
        verbose=0,
    )

    agent_config = RLAgentConfig(
        enabled=True,
        env_config=env_config,
        training=training_config,
    )

    agent = RLAgent.load(str(model_path))

    results = agent.evaluate(n_episodes=n_episodes)

    return results


def evaluate_all_models(
    model_dir: Path,
    symbol_list: list[str],
    n_episodes: int,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    """Evaluate all PPO models in a directory."""
    all_results = []

    for seed_dir in sorted(model_dir.glob("GPU_seed_*")):
        model_path = seed_dir / f"{args.agent}_final.zip"
        if not model_path.exists():
            model_path = seed_dir / f"{args.agent}_final"
        if not model_path.exists():
            logger.warning(f"No model found in {seed_dir}, skipping")
            continue

        try:
            meta_path = seed_dir / "training_meta.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

            logger.info(f"\nEvaluating {seed_dir.name}...")
            logger.info(f"  Reward: {meta.get('reward_scheme', 'unknown')}")
            logger.info(f"  Timesteps: {meta.get('timesteps', 'unknown'):,}")

            eval_results = evaluate_model(model_path, symbol_list, n_episodes, args)

            result = {
                "seed_dir": str(seed_dir.name),
                "model_path": str(model_path),
                "meta": meta,
                "evaluation": eval_results,
            }
            all_results.append(result)

            logger.info(f"  Mean reward: {eval_results.get('mean_reward', 0):.4f}")
            logger.info(f"  Mean final equity: ${eval_results.get('mean_final_equity', 0):,.2f}")
            logger.info(f"  Mean trade count: {eval_results.get('mean_trade_count', 0):.1f}")

        except Exception as e:
            logger.error(f"Failed to evaluate {seed_dir.name}: {e}")
            import traceback
            traceback.print_exc()

    return all_results


def main() -> int:
    args = parse_args()
    symbols = [s.strip() for s in args.symbols.split(",")]

    # Check GPU
    gpu_info = check_gpu()
    print(f"\n{'='*60}")
    print(f"  GPU Training Configuration")
    print(f"{'='*60}")
    print(f"  GPU available: {gpu_info['available']}")
    print(f"  Device: {gpu_info['device']}")
    if gpu_info['available']:
        print(f"  GPU: {gpu_info['gpu_name']}")
        print(f"  VRAM: {gpu_info['gpu_memory']:.1f} GB")
    print(f"  PyTorch: {gpu_info['torch_version']}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Timesteps per model: {args.timesteps:,}")
    print(f"  Number of models: {args.num_seeds}")
    print(f"  Reward scheme: {args.reward}")
    print(f"{'='*60}\n")

    if not gpu_info["available"]:
        print("  WARNING: No GPU detected. Training will be slow on CPU.")
        print("  For GPU training, install torch with CUDA support:\n")
        print("    pip install torch --index-url https://download.pytorch.org/whl/cu121\n")

    # Setup output directory
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = Path("state/rl_logs") / f"gpu_run_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Evaluate mode
    if args.evaluate:
        model_dir = Path(args.model_dir) if args.model_dir else output_dir
        if not model_dir.exists():
            print(f"ERROR: Model directory not found: {model_dir}")
            return 1

        results = evaluate_all_models(model_dir, symbols, args.eval_episodes, args)

        # Summary
        print(f"\n{'='*60}")
        print(f"  Evaluation Summary")
        print(f"{'='*60}")
        for r in results:
            seed = r["seed_dir"]
            eval_ = r["evaluation"]
            meta = r["meta"]
            print(f"\n  {seed}:")
            print(f"    Reward: {meta.get('reward_scheme', 'N/A')}")
            print(f"    Timesteps: {meta.get('timesteps', 'N/A'):,}")
            print(f"    Mean reward: {eval_.get('mean_reward', 0):.4f}")
            print(f"    Mean final equity: ${eval_.get('mean_final_equity', 0):,.2f}")
            print(f"    Mean trade count: {eval_.get('mean_trade_count', 0):.1f}")
        print(f"\n{'='*60}\n")

        return 0

    # Train mode
    all_results = []
    for seed in range(args.num_seeds):
        result = train_single_model(symbols, seed, args, output_dir)
        all_results.append(result)

        # Log to file
        log_path = output_dir / "training_log.json"
        log_data = json.loads(log_path.read_text()) if log_path.exists() else []
        log_data.append(result)
        log_path.write_text(json.dumps(log_data, indent=2), encoding="utf-8")

    # Save run config
    config_path = output_dir / "run_config.json"
    config = {
        "symbols": symbols,
        "agent": args.agent,
        "timesteps": args.timesteps,
        "num_seeds": args.num_seeds,
        "reward": args.reward,
        "interval": args.interval,
        "period": args.period,
        "learning_rate": args.learning_rate,
        "n_epochs": args.n_epochs,
        "batch_size": args.batch_size,
        "n_steps": args.n_steps,
        "gamma": args.gamma,
        "gae_lambda": args.gae_lambda,
        "clip_range": args.clip_range,
        "ent_coef": args.ent_coef,
        "vf_coef": args.vf_coef,
        "max_grad_norm": args.max_grad_norm,
        "reward_scale": args.reward_scale,
        "observer_window": args.observer_window,
        "max_episode_steps": args.max_episode_steps,
        "gpu_info": gpu_info,
        "timestamp": datetime.now().isoformat(),
    }
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # Summary
    print(f"\n{'='*60}")
    print(f"  Training Complete")
    print(f"{'='*60}")
    print(f"  Output directory: {output_dir}")
    for r in all_results:
        print(f"\n  Seed {r['seed']}:")
        print(f"    Time: {r['elapsed_seconds']:.1f}s")
        print(f"    Speed: {r['timesteps_per_second']:.0f} timesteps/sec")
        print(f"    Model: {r['model_path']}")
    print(f"\n{'='*60}\n")

    # Quick evaluation of best model
    if all_results:
        print("  Running quick evaluation on first model...")
        best_result = all_results[0]
        model_path = Path(best_result["model_path"])
        if model_path.exists():
            eval_results = evaluate_model(model_path, symbols, 10, args)
            print(f"\n  Quick evaluation results:")
            print(f"    Mean reward: {eval_results.get('mean_reward', 0):.4f}")
            print(f"    Mean final equity: ${eval_results.get('mean_final_equity', 0):,.2f}")
            print(f"    Mean trade count: {eval_results.get('mean_trade_count', 0):.1f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
