#!/usr/bin/env python3
"""RL Training Script for Autonomous Trading Agent.

Trains a DRL agent (PPO, A2C, SAC, TD3, or DDPG) on historical market data
using the Gymnasium TradingEnv environment.

Usage:
    python scripts/train_rl.py --symbols AAPL,SPY --agent PPO --timesteps 50000

    # Evaluate after training
    python scripts/train_rl.py --symbols AAPL --evaluate

    # Train with custom dates
    python scripts/train_rl.py --symbols AAPL,MSFT --start-date 2023-01-01 --end-date 2024-12-31
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train RL trading agent")
    parser.add_argument(
        "--symbols",
        type=str,
        default="AAPL",
        help="Comma-separated list of symbols to train on (default: AAPL)",
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="PPO",
        choices=["PPO", "A2C", "SAC", "TD3", "DDPG"],
        help="DRL agent type (default: PPO)",
    )
    parser.add_argument(
        "--episodes",
        type=int,
        default=100,
        help="Number of training episodes (default: 100)",
    )
    parser.add_argument(
        "--timesteps",
        type=int,
        default=50000,
        help="Total timesteps to train (default: 50000)",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=3e-4,
        help="Learning rate (default: 3e-4)",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date for training data (YYYY-MM-DD, default: 1 year ago)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date for training data (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="state/rl_logs",
        help="Output directory for trained models (default: state/rl_logs)",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Evaluate trained model instead of training",
    )
    parser.add_argument(
        "--eval-episodes",
        type=int,
        default=10,
        help="Number of evaluation episodes (default: 10)",
    )
    parser.add_argument(
        "--verbose",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Verbosity level (default: 1)",
    )
    return parser.parse_args()


def fetch_training_data(
    symbol: str,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    """Fetch historical market data with indicators."""
    from trading_bot.data import market_data

    if start_date is None:
        start_date = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    print(f"  Fetching data for {symbol}: {start_date} to {end_date}")

    daily_frame = market_data.fetch_bars(
        symbol,
        period="1y",
        interval="1d",
        start=start_date,
        end=end_date,
    )

    if daily_frame.empty:
        raise ValueError(f"No data fetched for {symbol}")

    print(f"  Fetched {len(daily_frame)} bars for {symbol}")
    return daily_frame


def train_agent(args: argparse.Namespace) -> int:
    """Train DRL agent on historical data."""
    from trading_bot.rl.agent import RLAgent, RLAgentConfig
    from trading_bot.rl.env import TradingConfig
    from trading_bot.rl.trainer import TrainingConfig as RLTrainingConfig

    symbols = [s.strip() for s in args.symbols.split(",")]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  RL Training Configuration")
    print(f"{'='*60}")
    print(f"  Agent type:  {args.agent}")
    print(f"  Symbols:     {', '.join(symbols)}")
    print(f"  Episodes:    {args.episodes}")
    print(f"  Timesteps:   {args.timesteps:,}")
    print(f"  Learning rate: {args.learning_rate}")
    print(f"  Output dir:  {output_dir}")
    print(f"{'='*60}\n")

    env_config = TradingConfig(
        symbols=symbols,
        bar_period="1y",
        bar_interval="1d",
        observer_window=10,
        starting_cash=100_000.0,
        fee_per_order=1.0,
        slippage_bps=5,
        max_positions=10,
        max_episode_steps=500,
    )

    training_config = RLTrainingConfig(
        env_config=env_config,
        model_type=args.agent,
        total_timesteps=args.timesteps,
        learning_rate=args.learning_rate,
        n_epochs=10,
        batch_size=64,
        n_steps=128,
        gamma=0.995,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.01,
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=args.verbose,
        log_dir=str(output_dir),
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

    print(f"Training {args.agent} on {', '.join(symbols)}...")
    print(f"  (This may take several minutes)\n")

    trainer = agent.train()

    model_path = output_dir / f"{args.agent}_final"
    agent.save(model_path)
    print(f"\n  Model saved to: {model_path}")

    print(f"\n{'='*60}")
    print(f"  Training complete!")
    print(f"{'='*60}\n")

    return 0


def evaluate_agent(args: argparse.Namespace) -> int:
    """Evaluate trained agent on historical data."""
    from trading_bot.rl.agent import RLAgent, RLAgentConfig
    from trading_bot.rl.env import TradingConfig
    from trading_bot.rl.trainer import TrainingConfig as RLTrainingConfig

    symbols = [s.strip() for s in args.symbols.split(",")]

    print(f"\n{'='*60}")
    print(f"  RL Agent Evaluation")
    print(f"{'='*60}")
    print(f"  Symbols:       {', '.join(symbols)}")
    print(f"  Eval episodes: {args.eval_episodes}")
    print(f"{'='*60}\n")

    for symbol in symbols:
        model_path = Path(args.output_dir) / f"PPO_final"
        if not model_path.exists():
            model_path = Path(args.output_dir) / f"{symbol}_PPO_final"

        if not model_path.exists():
            print(f"  WARNING: Model not found at {model_path}")
            continue

        print(f"\n  Evaluating {symbol} from {model_path}...")

        try:
            daily_frame = fetch_training_data(symbol, args.start_date, args.end_date)
        except Exception as e:
            print(f"  ERROR: Failed to fetch data for {symbol}: {e}")
            continue

        env_config = TradingConfig(
            symbols=[symbol],
            bar_period="1y",
            bar_interval="1d",
            observer_window=10,
            starting_cash=100_000.0,
            fee_per_order=1.0,
            slippage_bps=5,
            max_positions=10,
            max_episode_steps=500,
        )

        training_config = RLTrainingConfig(
            env_config=env_config,
            model_type="PPO",
            total_timesteps=50000,
            verbose=0,
        )

        agent_config = RLAgentConfig(
            enabled=True,
            env_config=env_config,
            training=training_config,
        )

        agent = RLAgent(config=agent_config)
        agent.load(model_path)

        results = agent.evaluate(n_episodes=args.eval_episodes)

        print(f"\n  Evaluation results for {symbol}:")
        print(f"    Mean reward:     {results.get('mean_reward', 0):.4f}")
        print(f"    Std reward:      {results.get('std_reward', 0):.4f}")
        print(f"    Mean final eq:   ${results.get('mean_final_equity', 0):,.2f}")
        print(f"    Min final eq:    ${results.get('min_final_equity', 0):,.2f}")
        print(f"    Max final eq:    ${results.get('max_final_equity', 0):,.2f}")

        initial = env_config.starting_cash
        final = results.get("mean_final_equity", 0)
        if initial > 0:
            ret = (final / initial - 1) * 100
            print(f"    Return:          {ret:+.2f}%")

    print(f"\n{'='*60}")
    print(f"  Evaluation complete!")
    print(f"{'='*60}\n")

    return 0


def main() -> int:
    args = parse_args()

    try:
        if args.evaluate:
            return evaluate_agent(args)
        else:
            return train_agent(args)
    except KeyboardInterrupt:
        print("\n  Training interrupted by user.")
        return 1
    except Exception as e:
        print(f"\n  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
