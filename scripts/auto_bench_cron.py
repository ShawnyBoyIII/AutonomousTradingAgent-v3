#!/usr/bin/env python3
"""Weekly cron job for automated alpha zoo benching.

Runs alpha zoo benching on a schedule and updates benching weights
for the scanner. Designed to be run via cron or task scheduler.

Usage:
    python scripts/auto_bench_cron.py --symbols SPY,AAPL,MSFT,GOOGL
    python scripts/auto_bench_cron.py --zoo all --min-ic-ir 0.15
    python scripts/auto_bench_cron.py --json --output state/bench_results.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from trading_bot.factors import AlphaZoo
from trading_bot.factors.bench import bench_zoo
from trading_bot.research.benching_weights import BenchingWeightsManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/bench_cron.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def run_benching(
    symbols: list[str],
    zoo: str = "all",
    lookback: int = 252,
    min_ic_ir: float = 0.1,
    max_ic_ir: float | None = None,
    output_path: str | None = None,
) -> dict:
    """Run alpha zoo benching and update weights.

    Args:
        symbols: Symbols to bench factors against.
        zoo: Zoo to bench (qlib, kakushadze, gtja, academic, all).
        lookback: Lookback period for IC calculation.
        min_ic_ir: Minimum IC IR to consider a factor viable.
        max_ic_ir: Maximum IC IR cap.
        output_path: Optional path to save results.

    Returns:
        Benching results dictionary.
    """
    import pandas as pd

    # Fetch market data
    logger.info("Fetching market data for: %s", ", ".join(symbols))
    frames = {}
    for symbol in symbols:
        try:
            from trading_bot.data.market_data import fetch_bars

            frame = fetch_bars(
                symbol=symbol,
                period="2y",
                interval="1d",
            )
            if frame is not None and len(frame) > 0:
                frames[symbol] = frame
                logger.info("  %s: %d bars", symbol, len(frame))
            else:
                logger.warning("  %s: No data returned", symbol)
        except Exception as e:
            logger.error("  %s: Failed to fetch data: %s", symbol, e)

    if not frames:
        logger.error("No market data available for benching")
        return {}

    # Use first symbol's frame for benching (factors are cross-sectional)
    frame = list(frames.values())[0]
    logger.info("Running benching on %d bars of %s data", len(frame), symbols[0])

    # Run benching
    if zoo == "all":
        results = {}
        for zoo_enum in AlphaZoo:
            logger.info("Benching zoo: %s", zoo_enum.value)
            zoo_results = bench_zoo(zoo_enum, frame, lookback=lookback)
            results[f"zoo_{zoo_enum.value}"] = zoo_results
    else:
        try:
            zoo_enum = AlphaZoo(zoo)
        except ValueError:
            logger.error("Invalid zoo: %s (use: qlib, kakushadze, gtja, academic, all)", zoo)
            return {}

        results = bench_zoo(zoo_enum, frame, lookback=lookback)

    # Update benching weights
    logger.info("Updating benching weights (min IC IR: %.2f)", min_ic_ir)
    manager = BenchingWeightsManager()
    updated = manager.update_from_benching(
        results,
        min_ic_ir=min_ic_ir,
        max_ic_ir=max_ic_ir,
    )
    logger.info("Updated %d factor weights", updated)

    # Save results
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(results, f, default=str, indent=2)
        logger.info("Saved benching results to %s", output_path)

    return results


def main():
    """Main entry point for cron job."""
    parser = argparse.ArgumentParser(description="Weekly alpha zoo benching cron job")
    parser.add_argument(
        "--symbols",
        type=str,
        default="SPY,AAPL,MSFT,GOOGL,AMZN",
        help="Comma-separated symbols to bench against",
    )
    parser.add_argument(
        "--zoo",
        type=str,
        default="all",
        help="Zoo to bench (qlib, kakushadze, gtja, academic, all)",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=252,
        help="Lookback period for IC calculation (default: 252)",
    )
    parser.add_argument(
        "--min-ic-ir",
        type=float,
        default=0.1,
        help="Minimum IC IR to consider a factor viable (default: 0.1)",
    )
    parser.add_argument(
        "--max-ic-ir",
        type=float,
        default=None,
        help="Maximum IC IR cap (default: None)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Path to save benching results (default: state/bench_results_YYYYMMDD.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without updating weights",
    )

    args = parser.parse_args()

    # Parse symbols
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        logger.error("No symbols provided")
        sys.exit(1)

    # Set output path if not provided
    output_path = args.output
    if not output_path:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        output_path = f"state/bench_results_{date_str}.json"

    logger.info("=" * 80)
    logger.info("Alpha Zoo Benching Cron Job")
    logger.info("Date: %s", datetime.now(timezone.utc).isoformat())
    logger.info("Symbols: %s", ", ".join(symbols))
    logger.info("Zoo: %s", args.zoo)
    logger.info("Lookback: %d", args.lookback)
    logger.info("=" * 80)

    try:
        results = run_benching(
            symbols=symbols,
            zoo=args.zoo,
            lookback=args.lookback,
            min_ic_ir=args.min_ic_ir,
            max_ic_ir=args.max_ic_ir,
            output_path=None if args.dry_run else output_path,
        )

        if results:
            logger.info("Benching completed successfully")
            logger.info("Results saved to: %s", output_path)
        else:
            logger.warning("No results returned from benching")
            sys.exit(1)

    except Exception as e:
        logger.error("Benching failed: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
