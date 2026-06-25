#!/usr/bin/env python3
"""RL Training Script for Autonomous Trading Agent.

Trains a DRL agent (PPO, A2C, SAC, TD3, or DDPG) on historical market data
using the Gymnasium TradingEnv environment.

Usage:
    python scripts/train_rl.py --symbols AAPL,SPY --agent PPO --episodes 100

    # With custom config
    python scripts/train_rl.py --config-path rl-config.yaml --symbols AAPL

    # Evaluate after training
    python scripts/train_rl.py --evaluate --symbols AAPL
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

# Add project root to path for imports
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


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
        "--feature-set",
        type=str,
        default="standard",
        choices=["standard", "extended"],
        help="Feature set to use (default: standard)",
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
        default=100000,
        help="Total timesteps to train (default: 100000)",
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
        default="trained_models",
        help="Output directory for trained models (default: trained_models)",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=None,
        help="Path to YAML config file",
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


def load_config(config_path: Path | None):
    """Load settings from config file or use defaults."""
    if config_path is None:
        from trading_bot.config.settings import Settings

        return Settings()

    from trading_bot.config.loader import load_settings

    return load_settings(config_path)


def fetch_training_data(
    symbol: str,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    """Fetch historical market data with indicators."""
    from trading_bot.data.indicators import (
        add_atr,
        add_bollinger_bands,
        add_cci,
        add_ema,
        add_macd,
        add_obv,
        add_rsi,
        add_sma,
        add_stochastic,
        add_vwap,
        add_williams_r,
        add_adx,
        add_atr_percent,
    )
    from trading_bot.data import market_data

    if start_date is None:
        start_date = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    print(f"Fetching data for {symbol}: {start_date} to {end_date}")

    daily_frame = market_data.fetch_bars(
        symbol,
        period="1y",
        interval="1d",
    )

    if daily_frame.empty:
        raise ValueError(f"No data fetched for {symbol}")

    print(f"Fetched {len(daily_frame)} bars for {symbol}")

    daily_frame = add_ema(daily_frame, period=20, column_name="ema_20")
    daily_frame = add_sma(daily_frame, period=50, column_name="sma_50")
    daily_frame = add_rsi(daily_frame, period=14)
    daily_frame = add_macd(daily_frame)
    daily_frame = add_bollinger_bands(daily_frame, period=20)
    daily_frame = add_stochastic(daily_frame, k_period=14, d_period=3)
    daily_frame = add_cci(daily_frame, period=20)
    daily_frame = add_williams_r(daily_frame, period=14)
    daily_frame = add_atr_percent(daily_frame, period=14)
    daily_frame = add_adx(daily_frame, period=14)
    daily_frame = add_obv(daily_frame)
    daily_frame = add_vwap(daily_frame)

    print(f"Added indicators. Columns: {list(daily_frame.columns)}")

    return daily_frame


def train_agent(args: argparse.Namespace):
    """Train DRL agent on historical data."""
    from trading_bot.rl.agent import RLAgent

    symbols = [s.strip() for s in args.symbols.split(",")]
    config = load_config(args.config_path)

    print(f"\n{'='*60}")
    print(f"RL Training Configuration")
    print(f"{'='*60}")
    print(f"Agent type: {args.agent}")
    print(f"Feature set: {args.feature_set}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Episodes: {args.episodes}")
    print(f"Timesteps: {args.timesteps:,}")
    print(f"Learning rate: {args.learning_rate}")
    print(f"Output directory: {args.output_dir}")
    print(f"{'='*60}\n")

    for symbol in symbols:
        print(f"\nTraining on {symbol}...")

        try:
            daily_frame = fetch_training_data(
                symbol,
                args.start_date,
                args.end_date,
            )
        except Exception as e:
            print(f"ERROR: Failed to fetch data for {symbol}: {e}")
            continue

        agent = RLAgent(
            agent_type=args.agent,
            feature_set=args.feature_set,
            learning_rate=args.learning_rate,
            verbose=args.verbose,
        )

        print(f"Starting training for {symbol}...")
        metrics = agent.train(
            daily_frame=daily_frame,
            ticker=symbol,
            episodes=args.episodes,
            timesteps=args.timesteps,
        )

        output_path = Path(args.output_dir) / f"rl_agent_{symbol}_{args.agent}.zip"
        agent.save(output_path)
        print(f"Model saved to: {output_path}")

        print(f"\nTraining metrics for {symbol}:")
        for key, value in metrics.items():
            print(f"  {key}: {value}")

    print(f"\n{'='*60}")
    print("Training complete!")
    print(f"{'='*60}\n")


def evaluate_agent(args: argparse.Namespace):
    """Evaluate trained agent on historical data."""
    from trading_bot.rl.agent import RLAgent

    symbols = [s.strip() for s in args.symbols.split(",")]

    print(f"\n{'='*60}")
    print(f"RL Agent Evaluation")
    print(f"{'='*60}")
    print(f"Symbols: {', '.join(symbols)}")
    print(f"Evaluation episodes: {args.eval_episodes}")
    print(f"{'='*60}\n")

    for symbol in symbols:
        model_path = Path(f"trained_models/rl_agent_{symbol}_PPO.zip")

        if not model_path.exists():
            print(f"WARNING: Model not found for {symbol} at {model_path}")
            continue

        print(f"\nEvaluating {symbol}...")

        try:
            daily_frame = fetch_training_data(symbol, None, None)
        except Exception as e:
            print(f"ERROR: Failed to fetch data for {symbol}: {e}")
            continue

        agent = RLAgent.load(
            path=model_path,
            feature_set="standard",
            verbose=0,
        )

        metrics = agent.evaluate(
            daily_frame=daily_frame,
            ticker=symbol,
            episodes=args.eval_episodes,
            initial_cash=10000.0,
            transaction_cost_bps=10.0,
        )

        print(f"\nEvaluation metrics for {symbol}:")
        print(f"  Average reward: {metrics['avg_reward']:.4f}")
        print(f"  Std reward: {metrics['std_reward']:.4f}")
        print(f"  Win rate: {metrics['win_rate']:.2%}")
        print(f"  Average final equity: ${metrics['avg_final_equity']:.2f}")
        print(f"  Initial cash: ${metrics['initial_cash']:.2f}")
        print(f"  Return: {(metrics['avg_final_equity'] / metrics['initial_cash'] - 1):.2%}")

    print(f"\n{'='*60}")
    print("Evaluation complete!")
    print(f"{'='*60}\n")


def main() -> int:
    args = parse_args()

    try:
        if args.evaluate:
            evaluate_agent(args)
        else:
            train_agent(args)
        return 0
    except KeyboardInterrupt:
        print("\nTraining interrupted by user.")
        return 1
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
