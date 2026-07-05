"""Tests for alpha factor benching weights manager."""

import json
from pathlib import Path

import pytest

from trading_bot.research.benching_weights import BenchingWeightsManager


class TestBenchingWeightsManager:
    """Tests for BenchingWeightsManager."""

    @pytest.fixture
    def manager(self, tmp_path):
        weights_path = str(tmp_path / "factor_bench_weights.json")
        return BenchingWeightsManager(weights_path)

    def test_load_empty(self, manager):
        """Test loading when no weights file exists."""
        assert manager.get_weights() == {}

    def test_save_and_load(self, manager):
        """Test saving and loading weights."""
        manager.set_weight("momentum_20d", 0.75)
        manager.set_weight("volume_5d", 0.50)
        manager.save()

        # Reload from disk
        manager._load()
        weights = manager.get_weights()
        assert weights["momentum_20d"] == 0.75
        assert weights["volume_5d"] == 0.50

    def test_get_weight(self, manager):
        """Test getting weight for a specific factor."""
        manager.set_weight("momentum_20d", 0.75)
        assert manager.get_weight("momentum_20d") == 0.75
        assert manager.get_weight("nonexistent") == 0.0

    def test_set_weight_positive(self, manager):
        """Test setting positive weight."""
        manager.set_weight("momentum_20d", 0.75)
        assert manager.get_weight("momentum_20d") == 0.75

    def test_set_weight_negative_removes(self, manager):
        """Test setting negative weight removes factor."""
        manager.set_weight("momentum_20d", 0.75)
        manager.set_weight("momentum_20d", -0.1)
        assert manager.get_weight("momentum_20d") == 0.0

    def test_update_from_benching_alive(self, manager):
        """Test updating weights from alive factors."""
        benching_results = {
            "qlib": {
                "factors": [
                    {
                        "factor_name": "momentum_20d",
                        "ic_ir": 0.35,
                        "categorization": "alive",
                    },
                    {
                        "factor_name": "mean_reversion_5d",
                        "ic_ir": -0.25,
                        "categorization": "reversed",
                    },
                ]
            }
        }

        updated = manager.update_from_benching(benching_results, min_ic_ir=0.1)
        assert updated == 1
        assert manager.get_weight("momentum_20d") == 0.35
        assert manager.get_weight("mean_reversion_5d") == 0.0

    def test_update_from_benching_min_filter(self, manager):
        """Test min_ic_ir filter."""
        benching_results = {
            "qlib": {
                "factors": [
                    {
                        "factor_name": "weak_factor",
                        "ic_ir": 0.05,
                        "categorization": "alive",
                    },
                    {
                        "factor_name": "strong_factor",
                        "ic_ir": 0.50,
                        "categorization": "alive",
                    },
                ]
            }
        }

        updated = manager.update_from_benching(benching_results, min_ic_ir=0.1)
        assert updated == 1
        assert manager.get_weight("weak_factor") == 0.0
        assert manager.get_weight("strong_factor") == 0.50

    def test_update_from_benching_max_filter(self, manager):
        """Test max_ic_ir filter."""
        benching_results = {
            "qlib": {
                "factors": [
                    {
                        "factor_name": "extreme_factor",
                        "ic_ir": 1.50,
                        "categorization": "alive",
                    },
                    {
                        "factor_name": "normal_factor",
                        "ic_ir": 0.30,
                        "categorization": "alive",
                    },
                ]
            }
        }

        updated = manager.update_from_benching(
            benching_results, min_ic_ir=0.1, max_ic_ir=1.0
        )
        assert updated == 1
        assert manager.get_weight("extreme_factor") == 0.0
        assert manager.get_weight("normal_factor") == 0.30

    def test_reset(self, manager, tmp_path):
        """Test resetting all weights."""
        manager.set_weight("momentum_20d", 0.75)
        weights_path = tmp_path / "factor_bench_weights.json"
        manager.weights_path = weights_path
        manager.save()

        manager.reset()
        assert manager.get_weights() == {}
        assert not weights_path.exists()

    def test_get_stats_empty(self, manager):
        """Test stats when no weights exist."""
        stats = manager.get_stats()
        assert stats["total_factors"] == 0
        assert stats["avg_weight"] == 0.0

    def test_get_stats_with_weights(self, manager):
        """Test stats with weights."""
        manager.set_weight("factor_a", 0.8)
        manager.set_weight("factor_b", 0.4)
        manager.set_weight("factor_c", 0.6)

        stats = manager.get_stats()
        assert stats["total_factors"] == 3
        assert stats["avg_weight"] == 0.6
        assert stats["max_weight"] == 0.8
        assert stats["min_weight"] == 0.4
        assert "factor_a" in stats["weights"]

    def test_persistence_across_instances(self, manager, tmp_path):
        """Test that weights persist across manager instances."""
        weights_path = tmp_path / "factor_bench_weights.json"
        manager.weights_path = weights_path

        # Save with first instance
        manager.set_weight("momentum_20d", 0.75)
        manager.save()

        # Load with new instance
        manager2 = BenchingWeightsManager(str(weights_path))
        assert manager2.get_weight("momentum_20d") == 0.75
