"""Tests for RL ensemble module (204 lines)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trading_bot.rl.ensemble import (
    EnsembleSignal,
    ModelSignal,
    RLEnsemble,
    discover_rl_models,
    load_discovered_symbols,
    save_discovered_symbols,
)


class TestModelSignal:
    def test_basic_signal(self):
        signal = ModelSignal(
            model_name="test_model",
            ticker="AAPL",
            action=1,
            confidence=0.8,
            trained_symbols=["AAPL", "GOOGL"],
        )
        assert signal.model_name == "test_model"
        assert signal.ticker == "AAPL"
        assert signal.action == 1
        assert signal.confidence == 0.8
        assert signal.trained_symbols == ["AAPL", "GOOGL"]


class TestEnsembleSignal:
    def test_basic_signal(self):
        signal = EnsembleSignal(
            ticker="AAPL",
            model_signals=[],
            agreement=0.0,
            majority_action=0,
            majority_confidence=0.0,
            unanimous=False,
        )
        assert signal.ticker == "AAPL"
        assert signal.agreement == 0.0
        assert signal.majority_action == 0
        assert signal.unanimous is False

    def test_with_signals(self):
        model_signals = [
            ModelSignal("model1", "AAPL", 1, 0.8, ["AAPL"]),
            ModelSignal("model2", "AAPL", 1, 0.7, ["AAPL"]),
        ]
        signal = EnsembleSignal(
            ticker="AAPL",
            model_signals=model_signals,
            agreement=1.0,
            majority_action=1,
            majority_confidence=0.75,
            unanimous=True,
        )
        assert len(signal.model_signals) == 2
        assert signal.agreement == 1.0
        assert signal.majority_action == 1
        assert signal.majority_confidence == 0.75
        assert signal.unanimous is True


class TestRLEnsemble:
    def test_load_no_models(self):
        ensemble = RLEnsemble([])
        names = ensemble.load()
        assert names == []
        assert ensemble.model_count == 0

    def test_load_fails_gracefully(self):
        with patch("trading_bot.rl.ensemble.RLAgent.load", side_effect=Exception("load failed")):
            ensemble = RLEnsemble(["/nonexistent/model.zip"])
            names = ensemble.load()
            assert names == []

    def test_load_success(self):
        mock_agent = MagicMock()
        mock_agent.config.model_path = Path("/path/to/model")

        with patch("trading_bot.rl.ensemble.RLAgent.load", return_value=mock_agent):
            with patch("trading_bot.rl.ensemble.rl_model_symbols", return_value=["AAPL"]):
                ensemble = RLEnsemble([Path("/path/to/model.zip")])
                names = ensemble.load()
                assert names == ["model.zip"]
                assert ensemble.model_count == 1

    def test_predict_no_models(self):
        ensemble = RLEnsemble([])
        ensemble.load()

        signal = ensemble.predict("AAPL", {})
        assert signal.ticker == "AAPL"
        assert signal.agreement == 0.0
        assert signal.majority_action == 0
        assert signal.unanimous is False

    def test_predict_single_model(self):
        mock_agent = MagicMock()
        mock_agent.predict_signal.return_value = (1, 0.8)
        mock_agent.config.model_path = Path("/path/to/model")

        with patch("trading_bot.rl.ensemble.rl_model_symbols", return_value=["AAPL"]):
            ensemble = RLEnsemble([Path("/path/to/model.zip")])
            ensemble._agents = [(mock_agent, "model1")]

            signal = ensemble.predict("AAPL", {})
            assert signal.ticker == "AAPL"
            assert signal.majority_action == 1
            assert signal.majority_confidence == 0.8
            assert signal.agreement == 1.0
            assert signal.unanimous is True

    def test_predict_multiple_models_agree(self):
        mock_agent1 = MagicMock()
        mock_agent1.predict_signal.return_value = (1, 0.8)
        mock_agent1.config.model_path = Path("/path/to/model1")

        mock_agent2 = MagicMock()
        mock_agent2.predict_signal.return_value = (1, 0.7)
        mock_agent2.config.model_path = Path("/path/to/model2")

        with patch("trading_bot.rl.ensemble.rl_model_symbols", return_value=["AAPL"]):
            ensemble = RLEnsemble([Path("/path/to/model1.zip"), Path("/path/to/model2.zip")])
            ensemble._agents = [(mock_agent1, "model1"), (mock_agent2, "model2")]

            signal = ensemble.predict("AAPL", {})
            assert signal.majority_action == 1
            assert signal.agreement == 1.0
            assert signal.unanimous is True
            assert signal.majority_confidence == 0.75

    def test_predict_multiple_models_disagree(self):
        mock_agent1 = MagicMock()
        mock_agent1.predict_signal.return_value = (1, 0.8)
        mock_agent1.config.model_path = Path("/path/to/model1")

        mock_agent2 = MagicMock()
        mock_agent2.predict_signal.return_value = (2, 0.7)
        mock_agent2.config.model_path = Path("/path/to/model2")

        mock_agent3 = MagicMock()
        mock_agent3.predict_signal.return_value = (2, 0.6)
        mock_agent3.config.model_path = Path("/path/to/model3")

        with patch("trading_bot.rl.ensemble.rl_model_symbols", return_value=["AAPL"]):
            ensemble = RLEnsemble([
                Path("/path/to/model1.zip"),
                Path("/path/to/model2.zip"),
                Path("/path/to/model3.zip"),
            ])
            ensemble._agents = [(mock_agent1, "model1"), (mock_agent2, "model2"), (mock_agent3, "model3")]

            signal = ensemble.predict("AAPL", {})
            assert signal.majority_action == 2
            assert signal.agreement == 2 / 3
            assert signal.unanimous is False

    def test_predict_model_failure(self):
        mock_agent1 = MagicMock()
        mock_agent1.predict_signal.return_value = (1, 0.8)
        mock_agent1.config.model_path = Path("/path/to/model1")

        mock_agent2 = MagicMock()
        mock_agent2.predict_signal.side_effect = Exception("prediction failed")
        mock_agent2.config.model_path = Path("/path/to/model2")

        with patch("trading_bot.rl.ensemble.rl_model_symbols", return_value=["AAPL"]):
            ensemble = RLEnsemble([
                Path("/path/to/model1.zip"),
                Path("/path/to/model2.zip"),
            ])
            ensemble._agents = [(mock_agent1, "model1"), (mock_agent2, "model2")]

            signal = ensemble.predict("AAPL", {})
            # Should still return a valid signal with only the successful model
            assert signal.ticker == "AAPL"
            assert len(signal.model_signals) == 1
            assert signal.majority_action == 1

    def test_predict_batch(self):
        mock_agent = MagicMock()
        mock_agent.predict_signal.return_value = (1, 0.8)
        mock_agent.config.model_path = Path("/path/to/model")

        with patch("trading_bot.rl.ensemble.rl_model_symbols", return_value=["AAPL"]):
            ensemble = RLEnsemble([Path("/path/to/model.zip")])
            ensemble._agents = [(mock_agent, "model1")]

            signals = ensemble.predict_batch(["AAPL", "GOOGL"], {})
            assert "AAPL" in signals
            assert "GOOGL" in signals
            assert signals["AAPL"].majority_action == 1
            assert signals["GOOGL"].majority_action == 1

    def test_model_names_property(self):
        mock_agent = MagicMock()
        with patch("trading_bot.rl.ensemble.rl_model_symbols", return_value=["AAPL"]):
            ensemble = RLEnsemble([Path("/path/to/model.zip")])
            ensemble._agents = [(mock_agent, "model1")]
            assert ensemble.model_names == ["model1"]

    def test_aggregate_empty(self):
        ensemble = RLEnsemble([])
        signal = ensemble._aggregate("AAPL", [])
        assert signal.ticker == "AAPL"
        assert signal.agreement == 0.0
        assert signal.majority_action == 0

    def test_aggregate_single_model(self):
        signals = [ModelSignal("model1", "AAPL", 1, 0.8, ["AAPL"])]
        ensemble = RLEnsemble([])
        result = ensemble._aggregate("AAPL", signals)
        assert result.majority_action == 1
        assert result.agreement == 1.0
        assert result.majority_confidence == 0.8
        assert result.unanimous is True

    def test_aggregate_majority_wins(self):
        signals = [
            ModelSignal("model1", "AAPL", 1, 0.8, ["AAPL"]),
            ModelSignal("model2", "AAPL", 1, 0.7, ["AAPL"]),
            ModelSignal("model3", "AAPL", 2, 0.6, ["AAPL"]),
        ]
        ensemble = RLEnsemble([])
        result = ensemble._aggregate("AAPL", signals)
        assert result.majority_action == 1
        assert result.agreement == 2 / 3
        assert result.unanimous is False


class TestDiscoverRLModels:
    def test_nonexistent_directory(self, tmp_path):
        models = discover_rl_models(str(tmp_path / "nonexistent"))
        assert models == []

    def test_no_zip_files(self, tmp_path):
        (tmp_path / "model.txt").touch()
        models = discover_rl_models(str(tmp_path))
        assert models == []

    def test_finds_ppo_zips(self, tmp_path):
        (tmp_path / "PPO_model.zip").touch()
        models = discover_rl_models(str(tmp_path))
        assert len(models) == 1
        assert "PPO_model.zip" in models[0]

    def test_ignores_non_ppo_zips(self, tmp_path):
        (tmp_path / "PPO_model.zip").touch()
        (tmp_path / "DQN_model.zip").touch()
        models = discover_rl_models(str(tmp_path))
        assert len(models) == 1

    def test_finds_zips_in_subdirectories(self, tmp_path):
        subdir = tmp_path / "sector_diversity"
        subdir.mkdir()
        (subdir / "PPO_trained.zip").touch()
        models = discover_rl_models(str(tmp_path))
        assert len(models) == 1

    def test_ignores_non_ppo_in_subdirectories(self, tmp_path):
        subdir = tmp_path / "sector_diversity"
        subdir.mkdir()
        (subdir / "PPO_trained.zip").touch()
        (subdir / "DQN_trained.zip").touch()
        models = discover_rl_models(str(tmp_path))
        assert len(models) == 1


class TestSaveLoadDiscoveredSymbols:
    def test_save_symbols(self, tmp_path):
        path = str(tmp_path / "discovered_symbols.txt")
        save_discovered_symbols(["AAPL", "googl", "msft"], path)

        content = Path(path).read_text()
        assert "AAPL" in content
        assert "GOOGL" in content
        assert "MSFT" in content

    def test_load_symbols(self, tmp_path):
        path = str(tmp_path / "discovered_symbols.txt")
        Path(path).write_text("AAPL\nGOOGL\nMSFT\n")

        symbols = load_discovered_symbols(path)
        assert symbols == ["AAPL", "GOOGL", "MSFT"]

    def test_load_nonexistent_file(self, tmp_path):
        symbols = load_discovered_symbols(str(tmp_path / "nonexistent.txt"))
        assert symbols == []

    def test_save_creates_parent_directory(self, tmp_path):
        nested = tmp_path / "nested" / "dir" / "symbols.txt"
        save_discovered_symbols(["AAPL"], str(nested))
        assert nested.exists()

    def test_load_ignores_empty_lines(self, tmp_path):
        path = str(tmp_path / "discovered_symbols.txt")
        Path(path).write_text("AAPL\n\nGOOGL\n\nMSFT\n")

        symbols = load_discovered_symbols(path)
        assert symbols == ["AAPL", "GOOGL", "MSFT"]

    def test_load_strips_whitespace(self, tmp_path):
        path = str(tmp_path / "discovered_symbols.txt")
        Path(path).write_text("  AAPL  \n  GOOGL  \n")

        symbols = load_discovered_symbols(path)
        assert symbols == ["AAPL", "GOOGL"]
