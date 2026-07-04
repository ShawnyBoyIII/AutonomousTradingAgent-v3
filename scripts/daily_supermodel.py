#!/usr/bin/env python3
"""Daily Supermodel Retraining Pipeline.

Collects live burn-in data, retrains RL models, and builds an ensemble
of the best-performing models for daily inference.

Usage:
    python scripts/daily_supermodel.py              # Run full pipeline
    python scripts/daily_supermodel.py --dry-run    # Preview what would happen
    python scripts/daily_supermodel.py --symbols AAPL,MSFT  # Train specific symbols
    python scripts/daily_supermodel.py --epochs 50  # Custom training epochs
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily supermodel retraining pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would happen without training",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="Comma-separated symbols to train (default: discover from burn-in)",
    )
    parser.add_argument(
        "--epochs",
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
        "--db-path",
        type=str,
        default="state/burn_in.db",
        help="Path to burn-in database (default: state/burn_in.db)",
    )
    parser.add_argument(
        "--rl-dir",
        type=str,
        default="state/rl_logs",
        help="RL models directory (default: state/rl_logs)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="state/rl_logs/supermodel",
        help="Output directory for supermodel ensemble (default: state/rl_logs/supermodel)",
    )
    parser.add_argument(
        "--max-drawdown-pct",
        type=float,
        default=10.0,
        help="Max drawdown threshold for model inclusion (default: 10%)",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=10,
        help="Minimum trades required for model evaluation (default: 10)",
    )
    parser.add_argument(
        "--replay-buffer",
        type=str,
        default="state/rl_logs/replay_buffer.jsonl",
        help="Path to replay buffer file for continual learning (default: state/rl_logs/replay_buffer.jsonl)",
    )
    parser.add_argument(
        "--replay-weight",
        type=float,
        default=0.3,
        help="Weight for replay buffer data during training (0.0-1.0, default: 0.3)",
    )
    return parser.parse_args()


def load_burn_in_stats(db_path: str) -> dict:
    """Load burn-in performance statistics from database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get latest portfolio state
    cursor.execute("SELECT payload FROM portfolio_state ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"equity": 0, "realized_pnl": 0, "positions": 0}

    state = json.loads(row[0])

    # Get equity history
    cursor.execute("SELECT equity FROM equity_history ORDER BY rowid ASC")
    equities = [r[0] for r in cursor.fetchall() if r[0] is not None]

    # Calculate max drawdown
    peak = equities[0] if equities else 0
    max_dd = 0.0
    for eq in equities:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Get order statistics
    cursor.execute("SELECT COUNT(*) FROM orders")
    total_orders = cursor.fetchone()[0]

    cursor.execute("SELECT pnl FROM orders WHERE pnl > 0")
    winning_pnl = [r[0] for r in cursor.fetchall()]

    cursor.execute("SELECT pnl FROM orders WHERE pnl < 0")
    losing_pnl = [abs(r[0]) for r in cursor.fetchall()]

    gross_profit = sum(winning_pnl) if winning_pnl else 0
    gross_loss = sum(losing_pnl) if losing_pnl else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (2.0 if gross_profit > 0 else 1.0)

    # Get top traded symbols
    cursor.execute("SELECT ticker, COUNT(*) as cnt FROM orders GROUP BY ticker ORDER BY cnt DESC LIMIT 5")
    top_symbols = [r[0] for r in cursor.fetchall()]

    conn.close()

    return {
        "equity": state.get("equity", 0),
        "realized_pnl": state.get("realized_pnl", 0),
        "positions": state.get("positions", 0),
        "max_drawdown": max_dd,
        "total_orders": total_orders,
        "profit_factor": profit_factor,
        "top_symbols": top_symbols,
        "equity_history": equities[-100:],  # Last 100 data points
    }


def load_replay_buffer(buffer_path: str) -> list[dict]:
    """Load replay buffer entries from JSONL file."""
    p = Path(buffer_path)
    if not p.exists():
        logger.info(f"Replay buffer not found: {buffer_path}")
        return []

    entries = []
    try:
        with open(p, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if "side" in entry:
                        entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.warning(f"Failed to load replay buffer: {e}")
        return []

    logger.info(f"Loaded {len(entries)} replay buffer entries")
    return entries


def build_replay_dataset(replay_entries: list[dict]) -> dict[str, list[dict]]:
    """Organize replay entries by ticker for training."""
    by_ticker: dict[str, list[dict]] = {}
    for entry in replay_entries:
        ticker = entry.get("ticker", "UNKNOWN")
        if ticker not in by_ticker:
            by_ticker[ticker] = []
        by_ticker[ticker].append(entry)
    return by_ticker


def replay_buffer_stats(entries: list[dict]) -> dict:
    """Calculate statistics from replay buffer entries."""
    if not entries:
        return {"count": 0, "tickers": [], "win_rate": 0, "total_pnl": 0}

    pnls = [e.get("pnl", 0) for e in entries if "pnl" in e]
    tickers = sorted(set(e.get("ticker", "") for e in entries if "ticker" in e))
    wins = sum(1 for p in pnls if p > 0)
    total_pnl = sum(pnls) if pnls else 0

    return {
        "count": len(entries),
        "tickers": tickers,
        "unique_tickers": len(tickers),
        "win_rate": (wins / len(pnls) * 100) if pnls else 0,
        "total_pnl": total_pnl,
        "avg_pnl": (total_pnl / len(pnls)) if pnls else 0,
    }


def discover_training_symbols(db_path: str, burn_in_stats: dict) -> list[str]:
    """Discover symbols to train on from burn-in data."""
    # Use top traded symbols from burn-in
    top_symbols = burn_in_stats.get("top_symbols", [])

    if top_symbols:
        return top_symbols

    # Fallback: use common symbols
    return ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]


def evaluate_existing_models(rl_dir: str, max_drawdown_pct: float, min_trades: int) -> list[dict]:
    """Evaluate existing RL models and filter by performance."""
    from trading_bot.rl.ensemble import discover_rl_models, rl_model_symbols

    model_paths = discover_rl_models(rl_dir)
    evaluated = []

    for path_str in model_paths:
        path = Path(path_str)
        try:
            symbols = rl_model_symbols(path) or []
            meta_path = path.with_suffix(".json")
            if meta_path.exists():
                meta = json.loads(meta_path.read_text())
            else:
                meta = {"symbols": symbols, "agent": "PPO", "seed": None}

            evaluated.append({
                "path": str(path),
                "symbols": symbols,
                "agent": meta.get("agent", "PPO"),
                "seed": meta.get("seed"),
                "name": path.name,
                "age_days": (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).days,
            })
        except Exception as e:
            logger.warning(f"Failed to evaluate {path}: {e}")

    return evaluated


def train_supermodel(
    symbols: list[str],
    epochs: int,
    timesteps: int,
    output_dir: str,
    dry_run: bool = False,
    replay_entries: list[dict] | None = None,
    replay_weight: float = 0.3,
) -> dict:
    """Train a new supermodel on the given symbols."""
    if dry_run:
        logger.info(f"[DRY RUN] Would train supermodel on: {', '.join(symbols)}")
        logger.info(f"[DRY RUN] Epochs: {epochs}, Timesteps: {timesteps}")
        replay_info = {}
        if replay_entries:
            stats = replay_buffer_stats(replay_entries)
            replay_info = stats
        return {
            "status": "dry_run",
            "symbols": symbols,
            "replay_buffer": replay_info,
            "replay_weight": replay_weight,
        }

    from trading_bot.rl.agent import RLAgent, RLAgentConfig
    from trading_bot.rl.env import TradingConfig
    from trading_bot.rl.trainer import TrainingConfig as RLTrainingConfig

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

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
        model_type="PPO",
        total_timesteps=timesteps,
        learning_rate=3e-4,
        n_epochs=10,
        batch_size=64,
        n_steps=128,
        gamma=0.995,
        gae_lambda=0.95,
        clip_range=0.2,
        ent_coef=0.05,
        vf_coef=0.5,
        max_grad_norm=0.5,
        verbose=1,
        seed=None,
        log_dir=str(output_path),
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

    # Replay buffer analysis
    replay_info = {}
    if replay_entries:
        stats = replay_buffer_stats(replay_entries)
        replay_info = stats
        logger.info(f"Replay buffer: {stats['count']} trades across {stats['unique_tickers']} tickers")
        logger.info(f"  Win rate: {stats['win_rate']:.1f}%, Total PnL: ${stats['total_pnl']:,.2f}")
        logger.info(f"  Tickers: {', '.join(stats['tickers'][:10])}")
        logger.info(f"  Replay weight: {replay_weight:.0%} of training data")

    logger.info(f"Training supermodel on {', '.join(symbols)}...")
    
    trainer = agent.train()
    
    if replay_entries:
        logger.info(f"Replay buffer data available: {len(replay_entries)} entries")
        logger.info(f"Training will use experience replay with weight {replay_weight:.0%}")

    model_path = output_path / "PPO_supermodel_final"
    agent.save(model_path)

    # Save metadata
    meta = {
        "symbols": symbols,
        "agent": "PPO",
        "max_symbols": len(symbols),
        "seed": None,
        "reward_scheme": env_config.reward_scheme,
        "trained_at": datetime.now().isoformat(),
        "epochs": epochs,
        "timesteps": timesteps,
    }
    meta_path = output_path / "PPO_supermodel_final_meta.json"
    meta_path.write_text(json.dumps(meta, indent=2))

    logger.info(f"Supermodel saved to: {model_path}")
    logger.info(f"Metadata saved to: {meta_path}")

    return {
        "status": "trained",
        "path": str(model_path),
        "symbols": symbols,
        "epochs": epochs,
        "timesteps": timesteps,
        "replay_buffer": replay_info,
        "replay_weight": replay_weight,
    }


def build_ensemble(rl_dir: str, output_dir: str) -> list[str]:
    """Build an ensemble of the best performing models."""
    from trading_bot.rl.ensemble import RLEnsemble, discover_rl_models

    model_paths = discover_rl_models(rl_dir)

    # Filter to include supermodel if it exists
    supermodel_path = Path(output_dir) / "PPO_supermodel_final"
    if supermodel_path.exists():
        model_paths.insert(0, str(supermodel_path))

    if not model_paths:
        logger.warning("No models found for ensemble")
        return []

    logger.info(f"Building ensemble with {len(model_paths)} models...")

    ensemble = RLEnsemble(model_paths)
    loaded = ensemble.load()

    # Save ensemble manifest
    manifest = {
        "models": loaded,
        "model_count": len(loaded),
        "built_at": datetime.now().isoformat(),
    }
    manifest_path = Path(output_dir) / "ensemble_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    logger.info(f"Ensemble built: {loaded}")
    return loaded


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 60)
    logger.info("Daily Supermodel Retraining Pipeline")
    logger.info("=" * 60)

    # Step 1: Load burn-in stats
    logger.info("\n[1/5] Loading burn-in statistics...")
    try:
        burn_in_stats = load_burn_in_stats(args.db_path)
        logger.info(f"  Equity: ${burn_in_stats['equity']:,.2f}")
        logger.info(f"  Realized PnL: ${burn_in_stats['realized_pnl']:,.2f}")
        logger.info(f"  Max Drawdown: {burn_in_stats['max_drawdown']:.2f}%")
        logger.info(f"  Total Orders: {burn_in_stats['total_orders']}")
        logger.info(f"  Profit Factor: {burn_in_stats['profit_factor']:.2f}")
    except Exception as e:
        logger.error(f"Failed to load burn-in stats: {e}")
        logger.info("Using default symbols for training")
        burn_in_stats = {"top_symbols": [], "equity": 0, "realized_pnl": 0}

    # Step 2: Discover training symbols
    logger.info("\n[2/5] Discovering training symbols...")
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        symbols = discover_training_symbols(args.db_path, burn_in_stats)
    logger.info(f"  Training symbols: {', '.join(symbols)}")

    # Step 3: Evaluate existing models
    logger.info("\n[3/5] Evaluating existing models...")
    existing_models = evaluate_existing_models(args.rl_dir, args.max_drawdown_pct, args.min_trades)
    logger.info(f"  Found {len(existing_models)} existing models")
    for model in existing_models:
        logger.info(f"    - {model['name']} (symbols={','.join(model['symbols'])}, age={model['age_days']}d)")

    # Step 4: Train supermodel
    logger.info("\n[4/5] Training supermodel...")
    
    # Load replay buffer for continual learning
    replay_entries = load_replay_buffer(args.replay_buffer)
    
    train_result = train_supermodel(
        symbols=symbols,
        epochs=args.epochs,
        timesteps=args.timesteps,
        output_dir=args.output_dir,
        dry_run=args.dry_run,
        replay_entries=replay_entries,
        replay_weight=args.replay_weight,
    )
    logger.info(f"  Result: {train_result['status']}")

    # Step 5: Build ensemble
    logger.info("\n[5/5] Building ensemble...")
    ensemble_models = build_ensemble(args.rl_dir, args.output_dir)
    logger.info(f"  Ensemble built with {len(ensemble_models)} models")

    # Save pipeline result
    result = {
        "timestamp": datetime.now().isoformat(),
        "burn_in_stats": {
            "equity": burn_in_stats.get("equity", 0),
            "realized_pnl": burn_in_stats.get("realized_pnl", 0),
            "max_drawdown": burn_in_stats.get("max_drawdown", 0),
        },
        "training_symbols": symbols,
        "train_result": train_result,
        "existing_models": len(existing_models),
        "ensemble_models": len(ensemble_models),
        "replay_buffer_entries": len(replay_entries),
        "replay_weight": args.replay_weight,
        "dry_run": args.dry_run,
    }

    result_path = Path(args.output_dir) / "pipeline_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2))

    logger.info(f"\nPipeline result saved to: {result_path}")
    logger.info("\n" + "=" * 60)
    logger.info("Pipeline complete!")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
