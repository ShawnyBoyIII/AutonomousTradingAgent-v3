#!/usr/bin/env python3
"""Live Data Collector for Continual Learning.

Monitors burn-in trades and collects market data + outcomes to build
a replay buffer for continual RL model training.

Usage:
    python scripts/live_data_collector.py              # Run once
    python scripts/live_data_collector.py --watch      # Continuously monitor
    python scripts/live_data_collector.py --buffer     # Show current buffer stats
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live data collector for continual learning")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously monitor for new trades",
    )
    parser.add_argument(
        "--buffer",
        action="store_true",
        help="Show current replay buffer statistics",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="state/burn_in.db",
        help="Path to burn-in database (default: state/burn_in.db)",
    )
    parser.add_argument(
        "--buffer-path",
        type=str,
        default="state/rl_logs/replay_buffer.jsonl",
        help="Path to replay buffer file (default: state/rl_logs/replay_buffer.jsonl)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Watch interval in seconds (default: 300)",
    )
    return parser.parse_args()


def get_last_processed_id(buffer_path: str) -> int:
    """Get the last processed order ID from the replay buffer."""
    p = Path(buffer_path)
    if not p.exists():
        return 0

    try:
        with open(p, "r") as f:
            lines = f.readlines()
            if not lines:
                return 0
            last_line = json.loads(lines[-1])
            return last_line.get("order_id", 0)
    except (json.JSONDecodeError, IndexError):
        return 0


def save_processed_id(buffer_path: str, order_id: int) -> None:
    """Save the last processed order ID to the replay buffer."""
    p = Path(buffer_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Append to buffer
    entry = {
        "order_id": order_id,
        "processed_at": datetime.now().isoformat(),
        "status": "collected",
    }

    with open(p, "a") as f:
        f.write(json.dumps(entry) + "\n")


def collect_trade_data(
    db_path: str,
    buffer_path: str,
    since_id: int = 0,
) -> list[dict]:
    """Collect new trade data from burn-in database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get new orders since last processed
    cursor.execute(
        "SELECT id, ticker, side, quantity, fill_price, fees, filled_at, pnl "
        "FROM orders WHERE id > ? ORDER BY filled_at ASC",
        (str(since_id),),
    )
    orders = cursor.fetchall()

    if not orders:
        conn.close()
        return []

    collected = []
    for order_row in orders:
        order_id, ticker, side, quantity, fill_price, fees, filled_at, pnl = order_row

        # Fetch market data around the fill time
        market_data = _fetch_market_context(ticker, filled_at)

        trade_entry = {
            "order_id": order_id,
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "fees": fees,
            "filled_at": filled_at,
            "pnl": pnl if pnl else 0.0,
            "market_data": market_data,
            "collected_at": datetime.now().isoformat(),
        }

        collected.append(trade_entry)

        # Save to buffer
        with open(buffer_path, "a") as f:
            f.write(json.dumps(trade_entry) + "\n")

        logger.info(f"Collected trade: {ticker} {side} {quantity} @ {fill_price} (PnL: {pnl})")

    conn.close()
    return collected


def _fetch_market_context(ticker: str, filled_at: str) -> dict:
    """Fetch market data context around a fill time."""
    try:
        from trading_bot.data import market_data as md

        # Parse fill time
        try:
            fill_dt = datetime.fromisoformat(filled_at)
        except (ValueError, TypeError):
            fill_dt = datetime.now()

        # Fetch daily data (1 year)
        start_date = (fill_dt - timedelta(days=365)).strftime("%Y-%m-%d")
        end_date = (fill_dt + timedelta(days=1)).strftime("%Y-%m-%d")

        daily_frame = md.fetch_bars(
            ticker,
            period="1y",
            interval="1d",
            start=start_date,
            end=end_date,
        )

        if daily_frame.empty:
            return {"error": "no_data"}

        # Extract features around fill time
        fill_idx = None
        for i, row in daily_frame.iterrows():
            if hasattr(row["timestamp"], "strftime") and row["timestamp"].strftime("%Y-%m-%d") == fill_dt.strftime("%Y-%m-%d"):
                fill_idx = i
                break

        if fill_idx is None:
            fill_idx = len(daily_frame) - 1

        fill_row = daily_frame.iloc[fill_idx]

        # Get surrounding bars
        window = 20
        start_idx = max(0, fill_idx - window)
        end_idx = min(len(daily_frame), fill_idx + 2)
        context_frame = daily_frame.iloc[start_idx:end_idx + 1]

        return {
            "fill_price": fill_row["close"],
            "fill_date": fill_row["timestamp"].strftime("%Y-%m-%d") if hasattr(fill_row["timestamp"], "strftime") else str(fill_row["timestamp"]),
            "high": float(fill_row["high"]),
            "low": float(fill_row["low"]),
            "volume": int(fill_row["volume"]),
            "context_bars": len(context_frame),
            "sma_20": float(context_frame["close"].mean()) if "close" in context_frame.columns else 0,
            "sma_50": float(context_frame["close"].mean()) if len(context_frame) > 1 and "close" in context_frame.columns else 0,
        }

    except Exception as e:
        logger.warning(f"Failed to fetch market context for {ticker}: {e}")
        return {"error": str(e)}


def show_buffer_stats(buffer_path: str) -> None:
    """Show replay buffer statistics."""
    p = Path(buffer_path)
    if not p.exists():
        print("Replay buffer is empty.")
        return

    with open(p, "r") as f:
        lines = f.readlines()

    if not lines:
        print("Replay buffer is empty.")
        return

    # Parse entries
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if not entries:
        print("Replay buffer is empty.")
        return

    # Calculate stats
    total_entries = len(entries)
    trades = [e for e in entries if "side" in e]
    processed = [e for e in entries if e.get("status") == "collected"]

    if trades:
        tickers = set(e["ticker"] for e in trades)
        buys = sum(1 for e in trades if e["side"] == "buy")
        sells = sum(1 for e in trades if e["side"] == "sell")
        pnls = [e.get("pnl", 0) for e in trades if "pnl" in e]
        win_rate = (sum(1 for p in pnls if p > 0) / len(pnls) * 100) if pnls else 0
        total_pnl = sum(pnls) if pnls else 0

        print("REPLAY BUFFER STATISTICS")
        print("=" * 60)
        print(f"  Total entries: {total_entries}")
        print(f"  Trade records: {len(trades)}")
        print(f"  Processed: {len(processed)}")
        print(f"  Unique tickers: {len(tickers)} ({', '.join(sorted(tickers))})")
        print(f"  Buys: {buys}, Sells: {sells}")
        print(f"  Win rate: {win_rate:.1f}%")
        print(f"  Total PnL: ${total_pnl:,.2f}")
        print(f"  First entry: {entries[0].get('collected_at', 'unknown')}")
        print(f"  Last entry: {entries[-1].get('collected_at', 'unknown')}")
    else:
        print(f"Replay buffer has {total_entries} entries (non-trade records)")


def watch_loop(db_path: str, buffer_path: str, interval: int) -> None:
    """Continuously watch for new trades."""
    logger.info(f"Starting watch loop (interval={interval}s)...")
    logger.info(f"DB: {db_path}")
    logger.info(f"Buffer: {buffer_path}")

    last_id = get_last_processed_id(buffer_path)

    while True:
        try:
            new_trades = collect_trade_data(db_path, buffer_path, since_id=last_id)

            if new_trades:
                logger.info(f"Collected {len(new_trades)} new trade(s)")
                last_id = max(int(t["order_id"]) for t in new_trades)
            else:
                logger.debug("No new trades")

        except Exception as e:
            logger.error(f"Error in watch loop: {e}")

        time.sleep(interval)


def main() -> int:
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.buffer:
        show_buffer_stats(args.buffer_path)
        return 0

    if args.watch:
        watch_loop(args.db_path, args.buffer_path, args.interval)
        return 0

    # Default: collect new data once
    logger.info("Collecting live trade data...")
    last_id = get_last_processed_id(args.buffer_path)

    collected = collect_trade_data(args.db_path, args.buffer_path, since_id=last_id)

    if collected:
        logger.info(f"Collected {len(collected)} new trade(s)")
        logger.info(f"Buffer saved to: {args.buffer_path}")
    else:
        logger.info("No new trades to collect")

    return 0


if __name__ == "__main__":
    sys.exit(main())
