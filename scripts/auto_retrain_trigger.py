#!/usr/bin/env python3
"""Auto-retrain trigger for new symbols.

Checks if new symbols have been added to the universe/watchlist that aren't
covered by any existing RL model, and triggers retraining if needed.

Usage:
    python scripts/auto_retrain_trigger.py              # Check and retrain if needed
    python scripts/auto_retrain_trigger.py --dry-run    # Preview what would happen
    python scripts/auto_retrain_trigger.py --force      # Force retrain regardless
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Auto-retrain trigger for new symbols")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would happen without training",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force retrain regardless of new symbols",
    )
    parser.add_argument(
        "--universe-path",
        type=str,
        default="state/universe.txt",
        help="Path to universe file (default: state/universe.txt)",
    )
    parser.add_argument(
        "--watchlist-path",
        type=str,
        default="state/watchlist.txt",
        help="Path to watchlist file (default: state/watchlist.txt)",
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
        help="Output directory for trained model (default: state/rl_logs/supermodel)",
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
        "--min-new-symbols",
        type=int,
        default=1,
        help="Minimum new symbols to trigger retrain (default: 1)",
    )
    return parser.parse_args()


def read_symbols_from_file(path: str) -> list[str]:
    """Read symbols from a text file (one per line, skip comments)."""
    p = Path(path)
    if not p.exists():
        return []
    
    symbols = []
    with open(p, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                symbols.append(line.upper())
    return symbols


def get_trained_symbols(rl_dir: str) -> set[str]:
    """Get all symbols covered by existing RL models."""
    from trading_bot.rl.ensemble import discover_rl_models, rl_model_symbols

    model_paths = discover_rl_models(rl_dir)
    trained: set[str] = set()
    
    for path_str in model_paths:
        path = Path(path_str)
        try:
            symbols = rl_model_symbols(path) or []
            trained.update(s.upper() for s in symbols)
        except Exception as e:
            logger.warning(f"Failed to read symbols from {path}: {e}")
    
    return trained


def discover_new_symbols(
    universe_path: str,
    watchlist_path: str,
    trained_symbols: set[str],
) -> list[str]:
    """Discover symbols not covered by any existing RL model."""
    universe_symbols = set(read_symbols_from_file(universe_path))
    watchlist_symbols = set(read_symbols_from_file(watchlist_path))
    
    all_symbols = universe_symbols | watchlist_symbols
    untrained = all_symbols - trained_symbols
    
    return sorted(untrained)


def trigger_retrain(
    new_symbols: list[str],
    dry_run: bool = False,
    epochs: int = 100,
    timesteps: int = 50000,
    output_dir: str = "state/rl_logs/supermodel",
) -> dict[str, Any]:
    """Trigger RL model retraining on new symbols."""
    if dry_run:
        logger.info(f"[DRY RUN] Would retrain on: {', '.join(new_symbols)}")
        return {
            "status": "dry_run",
            "symbols": new_symbols,
            "epochs": epochs,
            "timesteps": timesteps,
        }
    
    import subprocess
    
    cmd = [
        sys.executable,
        "scripts/daily_supermodel.py",
        "--symbols", ",".join(new_symbols),
        "--epochs", str(epochs),
        "--timesteps", str(timesteps),
        "--output-dir", output_dir,
    ]
    
    logger.info(f"Triggering retrain on: {', '.join(new_symbols)}")
    result = subprocess.run(cmd, capture_output=False)
    
    return {
        "status": "trained" if result.returncode == 0 else "failed",
        "symbols": new_symbols,
        "epochs": epochs,
        "timesteps": timesteps,
        "exit_code": result.returncode,
    }


def main() -> int:
    args = parse_args()
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    
    logger.info("=" * 60)
    logger.info("Auto-Retrain Trigger")
    logger.info("=" * 60)
    
    # Get current trained symbols
    logger.info("\n[1/3] Checking existing RL models...")
    trained_symbols = get_trained_symbols(args.rl_dir)
    logger.info(f"  Existing models cover: {', '.join(sorted(trained_symbols)) if trained_symbols else 'none'}")
    
    # Discover new symbols
    logger.info("\n[2/3] Discovering new symbols...")
    new_symbols = discover_new_symbols(
        args.universe_path,
        args.watchlist_path,
        trained_symbols,
    )
    logger.info(f"  New untrained symbols: {', '.join(new_symbols) if new_symbols else 'none'}")
    
    # Trigger retrain if needed
    logger.info("\n[3/3] Checking retrain trigger...")
    if args.force or len(new_symbols) >= args.min_new_symbols:
        logger.info(f"  Trigger: {'FORCE' if args.force else f'{len(new_symbols)} new symbols >= {args.min_new_symbols}'}")
        result = trigger_retrain(
            new_symbols,
            dry_run=args.dry_run,
            epochs=args.epochs,
            timesteps=args.timesteps,
            output_dir=args.output_dir,
        )
        logger.info(f"  Result: {result['status']}")
        
        if result["status"] == "dry_run":
            logger.info("\n[Dry run complete - no training performed]")
        elif result["status"] == "trained":
            logger.info("\n[Retrain complete - new model ready]")
        else:
            logger.error("\n[Retrain failed - check logs]")
            return 1
    else:
        logger.info(f"  No retrain needed ({len(new_symbols)} new symbols < {args.min_new_symbols} threshold)")
        logger.info("\n[All symbols already covered by existing models]")
    
    logger.info("\n" + "=" * 60)
    logger.info("Auto-retrain check complete!")
    logger.info("=" * 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
