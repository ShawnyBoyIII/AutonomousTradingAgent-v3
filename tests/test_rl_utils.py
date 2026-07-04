"""Tests for RL utils module (19 lines)."""

from __future__ import annotations

from pathlib import Path

from trading_bot.rl.utils import rl_model_meta_path, rl_model_symbols


class TestRlModelMetaPath:
    def test_zip_suffix(self, tmp_path):
        model_path = tmp_path / "PPO_model.zip"
        model_path.touch()
        meta = rl_model_meta_path(model_path)
        assert meta.name == "PPO_model_meta.json"

    def test_non_zip_suffix(self, tmp_path):
        model_path = tmp_path / "model.pt"
        model_path.touch()
        meta = rl_model_meta_path(model_path)
        assert meta.name == "model_meta.json"

    def test_directory_path(self, tmp_path):
        model_path = tmp_path / "model_dir"
        model_path.mkdir()
        meta = rl_model_meta_path(model_path)
        assert meta.name == "model_dir_meta.json"

    def test_meta_in_same_directory(self, tmp_path):
        model_path = tmp_path / "models" / "PPO_model.zip"
        model_path.parent.mkdir(parents=True)
        model_path.touch()
        meta = rl_model_meta_path(model_path)
        assert meta.parent == model_path.parent


class TestRlModelSymbols:
    def test_existing_meta(self, tmp_path):
        meta_path = tmp_path / "PPO_model_meta.json"
        meta_path.write_text('{"symbols": ["AAPL", "GOOGL"]}')
        model_path = tmp_path / "PPO_model.zip"

        symbols = rl_model_symbols(model_path)
        assert symbols == ["AAPL", "GOOGL"]

    def test_nonexistent_meta(self, tmp_path):
        model_path = tmp_path / "PPO_model.zip"
        symbols = rl_model_symbols(model_path)
        assert symbols is None

    def test_uppercase_symbols(self, tmp_path):
        meta_path = tmp_path / "PPO_model_meta.json"
        meta_path.write_text('{"symbols": ["aapl", "googl"]}')
        model_path = tmp_path / "PPO_model.zip"

        symbols = rl_model_symbols(model_path)
        assert symbols == ["AAPL", "GOOGL"]

    def test_stripped_symbols(self, tmp_path):
        meta_path = tmp_path / "PPO_model_meta.json"
        meta_path.write_text('{"symbols": ["  AAPL  ", "  GOOGL  "]}')
        model_path = tmp_path / "PPO_model.zip"

        symbols = rl_model_symbols(model_path)
        assert symbols == ["AAPL", "GOOGL"]

    def test_empty_symbols_list(self, tmp_path):
        meta_path = tmp_path / "PPO_model_meta.json"
        meta_path.write_text('{"symbols": []}')
        model_path = tmp_path / "PPO_model.zip"

        symbols = rl_model_symbols(model_path)
        assert symbols == []

    def test_missing_symbols_key(self, tmp_path):
        meta_path = tmp_path / "PPO_model_meta.json"
        meta_path.write_text('{"other_key": "value"}')
        model_path = tmp_path / "PPO_model.zip"

        symbols = rl_model_symbols(model_path)
        assert symbols == []

    def test_non_json_file(self, tmp_path):
        meta_path = tmp_path / "PPO_model_meta.json"
        meta_path.write_text("not valid json")
        model_path = tmp_path / "PPO_model.zip"

        import pytest
        with pytest.raises(Exception):
            rl_model_symbols(model_path)
