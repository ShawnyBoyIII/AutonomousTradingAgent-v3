"""Pattern mining logic."""
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

from trading_bot.data.data_store import DataStoreManifest, read_bars

logger = logging.getLogger(__name__)

def mine_patterns(
    store_root: Path,
    manifest_db: Path,
    lookback_days: int = 90
) -> list[dict[str, Any]]:
    """Scan historical EOD data for repeating setups.

    Args:
        store_root: Path to the Parquet data root
        manifest_db: Path to the SQLite manifest DB
        lookback_days: How many days back to scan

    Returns:
        List of mined pattern dictionaries.
    """
    manifest = DataStoreManifest(db_path=manifest_db)
    symbols = manifest.symbols()

    if not symbols:
        logger.warning("No symbols found in EOD data store. Has eod-fetch run?")
        return []

    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    patterns = []

    # Very basic patterns:
    # 1. 3 consecutive down days (oversold) -> Next day return
    # 2. 3 consecutive up days (overbought) -> Next day return
    # 3. Gap down > 2% -> Next day return
    # 4. Gap up > 2% -> Next day return

    # Store aggregated results
    pattern_stats = {
        "3_down_days": {"hits": 0, "total_return": 0.0, "wins": 0},
        "3_up_days": {"hits": 0, "total_return": 0.0, "wins": 0},
        "gap_down_2pct": {"hits": 0, "total_return": 0.0, "wins": 0},
        "gap_up_2pct": {"hits": 0, "total_return": 0.0, "wins": 0},
    }

    for symbol in symbols:
        # Load 1d bars
        df = read_bars(
            symbol=symbol,
            interval="1d",
            start=start_date,
            end=end_date,
            root=store_root
        )

        if df.empty or len(df) < 5:
            continue

        df = df.sort_values("window_start").reset_index(drop=True)

        # Calculate features
        df["return"] = df["close"] / df["close"].shift(1) - 1.0
        df["gap"] = df["open"] / df["close"].shift(1) - 1.0
        df["is_up"] = df["return"] > 0
        df["is_down"] = df["return"] < 0

        # Future 1-day return (what happens next day)
        df["fwd_return_1d"] = df["return"].shift(-1)

        for i in range(3, len(df) - 1): # -1 to have a fwd_return
            row = df.iloc[i]
            prev1 = df.iloc[i-1]
            prev2 = df.iloc[i-2]
            fwd_ret = row["fwd_return_1d"]

            if pd.isna(fwd_ret):
                continue

            is_win = fwd_ret > 0

            # Pattern 1: 3 down days
            if row["is_down"] and prev1["is_down"] and prev2["is_down"]:
                pattern_stats["3_down_days"]["hits"] += 1
                pattern_stats["3_down_days"]["total_return"] += fwd_ret
                if is_win:
                    pattern_stats["3_down_days"]["wins"] += 1

            # Pattern 2: 3 up days
            if row["is_up"] and prev1["is_up"] and prev2["is_up"]:
                pattern_stats["3_up_days"]["hits"] += 1
                pattern_stats["3_up_days"]["total_return"] += fwd_ret
                if is_win:
                    pattern_stats["3_up_days"]["wins"] += 1

            # Pattern 3: Gap down > 2%
            if row["gap"] < -0.02:
                pattern_stats["gap_down_2pct"]["hits"] += 1
                pattern_stats["gap_down_2pct"]["total_return"] += fwd_ret
                if is_win:
                    pattern_stats["gap_down_2pct"]["wins"] += 1

            # Pattern 4: Gap up > 2%
            if row["gap"] > 0.02:
                pattern_stats["gap_up_2pct"]["hits"] += 1
                pattern_stats["gap_up_2pct"]["total_return"] += fwd_ret
                if is_win:
                    pattern_stats["gap_up_2pct"]["wins"] += 1

    # Format output
    for p_name, stats in pattern_stats.items():
        hits = stats["hits"]
        if hits > 0:
            avg_return = stats["total_return"] / hits
            win_rate = stats["wins"] / hits
            patterns.append({
                "name": p_name,
                "hits": hits,
                "wins": stats["wins"],
                "avg_return": avg_return,
                "win_rate": win_rate,
                "description": f"{p_name} across {len(symbols)} symbols over last {lookback_days} days"
            })

    return patterns
