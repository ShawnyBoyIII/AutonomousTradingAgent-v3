"""Tests for the pattern miner."""
import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trading_bot.patterns.miner import mine_patterns
from trading_bot.patterns.digest import generate_digest
from trading_bot.research.store import ResearchStore
from trading_bot.research.models import Hypothesis


def test_mine_patterns_empty(tmp_path: Path):
    """Test mining handles empty data store gracefully."""
    manifest_db = tmp_path / "data_store.db"
    store_root = tmp_path / "data_store"

    # Initialize empty manifest
    from trading_bot.data.data_store import DataStoreManifest
    manifest = DataStoreManifest(db_path=manifest_db)

    patterns = mine_patterns(store_root=store_root, manifest_db=manifest_db)
    assert patterns == []


def test_mine_patterns_basic(tmp_path: Path):
    """Test that mining finds basic patterns when data exists."""
    manifest_db = tmp_path / "data_store.db"
    store_root = tmp_path / "data_store"

    from trading_bot.data.data_store import DataStoreManifest, write_bars
    manifest = DataStoreManifest(db_path=manifest_db)

    # Create synthetic price data for "AAPL"
    # Make it have 3 consecutive down days
    today = date.today()
    days = [today - timedelta(days=i) for i in range(10, 0, -1)]

    # Prices: 100, 99, 98, 97 (3 down days), 105 (big up day next = win)
    closes = [100.0, 99.0, 98.0, 97.0, 105.0, 106.0, 107.0, 108.0, 109.0, 110.0]
    opens = [100.0, 99.5, 98.5, 97.5, 97.0, 106.0, 107.0, 108.0, 109.0, 110.0]

    # Needs to match what data_store expects in read_bars: `window_start` in ns
    window_starts = [int(pd.Timestamp(d).timestamp() * 1e9) for d in days]

    df = pd.DataFrame({
        "open": opens,
        "close": closes,
        "window_start": window_starts,
    })

    # Write to Parquet using the official helper to match partition layout
    for i, row in df.iterrows():
        single_row_df = pd.DataFrame([row])
        write_bars(
            df=single_row_df,
            symbol="AAPL",
            interval="1d",
            as_of_date=days[i],
            root=store_root,
            manifest=manifest
        )

    # Now mine
    patterns = mine_patterns(store_root=store_root, manifest_db=manifest_db, lookback_days=30)

    # We should have at least the '3_down_days' pattern triggered
    assert len(patterns) > 0
    names = [p["name"] for p in patterns]

    assert "3_down_days" in names

    # Check the down days stats
    down_stats = next(p for p in patterns if p["name"] == "3_down_days")
    assert down_stats["hits"] >= 1
    assert down_stats["wins"] >= 1 # The next day went from 97->105
    assert down_stats["win_rate"] == 1.0


def test_generate_digest(tmp_path: Path):
    """Test generating digest files and writing to DB."""
    output_dir = tmp_path / "patterns"
    research_db = tmp_path / "research.db"

    patterns = [
        {
            "name": "test_pattern",
            "hits": 15,
            "win_rate": 0.60, # High enough to trigger DB write (> 0.55)
            "avg_return": 0.05,
            "description": "Test pattern"
        },
        {
            "name": "ignored_pattern",
            "hits": 5,
            "win_rate": 0.50, # Low win rate, ignored by DB
            "avg_return": 0.01,
            "description": "Ignored pattern"
        }
    ]

    generate_digest(patterns, output_dir=output_dir, research_db_path=str(research_db))

    # Check files exist
    assert (output_dir / "digest.json").exists()
    assert (output_dir / "digest.md").exists()

    # Check JSON content
    with open(output_dir / "digest.json") as f:
        data = json.load(f)
        assert len(data) == 2
        # Should be sorted by win_rate descending
        assert data[0]["name"] == "test_pattern"

    # Check Markdown content
    with open(output_dir / "digest.md") as f:
        content = f.read()
        assert "test_pattern" in content
        assert "ignored_pattern" in content
        assert "60.0%" in content

    # Check Research DB content
    store = ResearchStore(db_path=str(research_db))

    # There's no easy get_all_hypotheses on ResearchStore, but we can query sqlite
    import sqlite3
    conn = sqlite3.connect(research_db)
    rows = conn.execute("SELECT title, expected_outcome FROM hypotheses").fetchall()

    assert len(rows) == 1
    assert rows[0][0] == "Mined Pattern: test_pattern"
    assert "60.0%" in rows[0][1]
