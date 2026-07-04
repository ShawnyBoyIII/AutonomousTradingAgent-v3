#!/usr/bin/env python3
"""Tests for replay buffer loading and supermodel training integration."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.daily_supermodel import (
    build_replay_dataset,
    load_replay_buffer,
    replay_buffer_stats,
    train_supermodel,
)


class TestLoadReplayBuffer:
    def test_empty_file(self, tmp_path: Path) -> None:
        buffer_path = tmp_path / "replay_buffer.jsonl"
        buffer_path.write_text("")
        result = load_replay_buffer(str(buffer_path))
        assert result == []

    def test_missing_file(self, tmp_path: Path) -> None:
        buffer_path = tmp_path / "nonexistent.jsonl"
        result = load_replay_buffer(str(buffer_path))
        assert result == []

    def test_load_trade_entries(self, tmp_path: Path) -> None:
        buffer_path = tmp_path / "replay_buffer.jsonl"
        entries = [
            {"order_id": 1, "ticker": "AAPL", "side": "buy", "pnl": 100.0},
            {"order_id": 2, "ticker": "MSFT", "side": "sell", "pnl": -50.0},
        ]
        buffer_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = load_replay_buffer(str(buffer_path))
        assert len(result) == 2
        assert result[0]["ticker"] == "AAPL"
        assert result[1]["ticker"] == "MSFT"

    def test_skips_non_trade_entries(self, tmp_path: Path) -> None:
        buffer_path = tmp_path / "replay_buffer.jsonl"
        mixed = [
            {"order_id": 1, "ticker": "AAPL", "side": "buy", "pnl": 100.0},
            {"order_id": 2, "processed_at": "2026-01-01", "status": "collected"},
        ]
        buffer_path.write_text("\n".join(json.dumps(e) for e in mixed) + "\n")
        result = load_replay_buffer(str(buffer_path))
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"

    def test_skips_corrupt_lines(self, tmp_path: Path) -> None:
        buffer_path = tmp_path / "replay_buffer.jsonl"
        buffer_path.write_text(
            '{"ticker": "AAPL", "side": "buy", "pnl": 100.0}\n'
            "this is not json\n"
            '{"ticker": "MSFT", "side": "sell", "pnl": -50.0}\n'
        )
        result = load_replay_buffer(str(buffer_path))
        assert len(result) == 2

    def test_empty_lines_ignored(self, tmp_path: Path) -> None:
        buffer_path = tmp_path / "replay_buffer.jsonl"
        buffer_path.write_text("\n\n{\"ticker\": \"AAPL\", \"side\": \"buy\"}\n\n")
        result = load_replay_buffer(str(buffer_path))
        assert len(result) == 1


class TestBuildReplayDataset:
    def test_groups_by_ticker(self) -> None:
        entries = [
            {"ticker": "AAPL", "side": "buy"},
            {"ticker": "MSFT", "side": "sell"},
            {"ticker": "AAPL", "side": "sell"},
        ]
        result = build_replay_dataset(entries)
        assert set(result.keys()) == {"AAPL", "MSFT"}
        assert len(result["AAPL"]) == 2
        assert len(result["MSFT"]) == 1

    def test_empty_input(self) -> None:
        result = build_replay_dataset([])
        assert result == {}


class TestReplayBufferStats:
    def test_empty_entries(self) -> None:
        result = replay_buffer_stats([])
        assert result["count"] == 0
        assert result["win_rate"] == 0

    def test_calculates_stats(self) -> None:
        entries = [
            {"ticker": "AAPL", "pnl": 100.0},
            {"ticker": "AAPL", "pnl": -50.0},
            {"ticker": "MSFT", "pnl": 200.0},
        ]
        result = replay_buffer_stats(entries)
        assert result["count"] == 3
        assert result["unique_tickers"] == 2
        assert result["win_rate"] == pytest.approx(66.67, abs=0.1)
        assert result["total_pnl"] == 250.0
        assert result["avg_pnl"] == pytest.approx(83.33, abs=0.1)
        assert "AAPL" in result["tickers"]
        assert "MSFT" in result["tickers"]


class TestTrainSupermodelReplay:
    def test_dry_run_with_replay(self, tmp_path: Path) -> None:
        replay_path = tmp_path / "replay_buffer.jsonl"
        replay_path.write_text(
            json.dumps({"ticker": "AAPL", "side": "buy", "pnl": 100.0}) + "\n"
        )
        output_dir = tmp_path / "output"

        result = train_supermodel(
            symbols=["AAPL"],
            epochs=10,
            timesteps=1000,
            output_dir=str(output_dir),
            dry_run=True,
            replay_entries=[{"ticker": "AAPL", "side": "buy", "pnl": 100.0}],
            replay_weight=0.3,
        )

        assert result["status"] == "dry_run"
        assert result["symbols"] == ["AAPL"]
        assert "replay_buffer" in result
        assert result["replay_weight"] == 0.3

    def test_dry_run_without_replay(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        result = train_supermodel(
            symbols=["AAPL"],
            epochs=10,
            timesteps=1000,
            output_dir=str(output_dir),
            dry_run=True,
        )
        assert result["status"] == "dry_run"
        assert result["replay_buffer"] == {}
        assert result["replay_weight"] == 0.3

    @patch("trading_bot.rl.agent.RLAgent")
    def test_trains_with_replay(self, mock_agent_cls, tmp_path: Path) -> None:
        mock_agent = MagicMock()
        mock_agent_cls.return_value = mock_agent
        mock_trainer = MagicMock()
        mock_agent.train.return_value = mock_trainer

        output_dir = tmp_path / "output"
        replay_entries = [
            {"ticker": "AAPL", "side": "buy", "pnl": 100.0},
            {"ticker": "MSFT", "side": "sell", "pnl": -50.0},
        ]

        result = train_supermodel(
            symbols=["AAPL", "MSFT"],
            epochs=10,
            timesteps=1000,
            output_dir=str(output_dir),
            dry_run=False,
            replay_entries=replay_entries,
            replay_weight=0.5,
        )

        assert result["status"] == "trained"
        assert result["replay_buffer"]["count"] == 2
        assert result["replay_weight"] == 0.5
        mock_agent.train.assert_called_once()
