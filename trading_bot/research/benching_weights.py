"""Alpha factor benching weights manager.

Manages persistent IC IR weights for alpha factors used in scanner scoring.
Integrates with the alpha zoo benching system and scanner configuration.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class BenchingWeightsManager:
    """Manages alpha factor benching weights for persistent scanner scoring.

    Loads, saves, and updates IC IR weights computed by the alpha zoo benching system.
    Weights are used by the scanner to weight alpha factors in composite scoring.
    """

    def __init__(self, weights_path: str | None = None):
        if weights_path is None:
            weights_path = "state/factor_bench_weights.json"
        self.weights_path = Path(weights_path)
        self.weights_path.parent.mkdir(parents=True, exist_ok=True)
        self._weights: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        """Load weights from disk."""
        if not self.weights_path.exists():
            logger.info("No benching weights file found at %s", self.weights_path)
            return

        try:
            with open(self.weights_path) as f:
                data = json.load(f)
            # Only keep positive IC IR weights
            self._weights = {
                name: weight
                for name, weight in data.items()
                if isinstance(weight, (int, float)) and weight > 0
            }
            logger.info("Loaded %d benching weights from %s", len(self._weights), self.weights_path)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load benching weights: %s", e)
            self._weights = {}

    def save(self) -> None:
        """Save current weights to disk."""
        try:
            with open(self.weights_path, "w") as f:
                json.dump(self._weights, f, indent=2)
            logger.info("Saved %d benching weights to %s", len(self._weights), self.weights_path)
        except IOError as e:
            logger.error("Failed to save benching weights: %s", e)

    def get_weights(self) -> dict[str, float]:
        """Get all weights."""
        return dict(self._weights)

    def get_weight(self, factor_name: str) -> float:
        """Get weight for a specific factor."""
        return self._weights.get(factor_name, 0.0)

    def set_weight(self, factor_name: str, weight: float) -> None:
        """Set weight for a specific factor."""
        if weight > 0:
            self._weights[factor_name] = weight
        elif factor_name in self._weights:
            del self._weights[factor_name]

    def update_from_benching(
        self,
        benching_results: dict[str, Any],
        min_ic_ir: float = 0.1,
        max_ic_ir: float | None = None,
    ) -> int:
        """Update weights from alpha zoo benching results.

        Args:
            benching_results: Results from bench_zoo() or bench_all().
            min_ic_ir: Minimum IC IR to consider a factor viable.
            max_ic_ir: Maximum IC IR cap (None for no cap).

        Returns:
            Number of weights updated.
        """
        updated = 0

        for zoo_name, zoo_data in benching_results.items():
            if not isinstance(zoo_data, dict):
                continue

            factors = zoo_data.get("factors", [])
            for factor_data in factors:
                factor_name = factor_data.get("factor_name", "")
                ic_ir = factor_data.get("ic_ir", 0)
                categorization = factor_data.get("categorization", "")

                # Only use alive factors with positive IC IR
                if categorization != "alive" or ic_ir <= 0:
                    continue

                # Apply filters
                if ic_ir < min_ic_ir:
                    continue
                if max_ic_ir is not None and ic_ir > max_ic_ir:
                    continue

                # Normalize IC IR to 0-1 range for weights
                normalized = min(ic_ir / 1.0, 1.0)

                self.set_weight(factor_name, normalized)
                updated += 1

        if updated > 0:
            self.save()

        logger.info(
            "Updated %d benching weights (min IC IR: %.2f, max: %s)",
            updated,
            min_ic_ir,
            max_ic_ir or "unbounded",
        )
        return updated

    def reset(self) -> None:
        """Reset all weights."""
        self._weights = {}
        if self.weights_path.exists():
            self.weights_path.unlink()
        logger.info("Reset all benching weights")

    def get_stats(self) -> dict[str, Any]:
        """Get weight statistics."""
        if not self._weights:
            return {
                "total_factors": 0,
                "avg_weight": 0.0,
                "max_weight": 0.0,
                "min_weight": 0.0,
            }

        weights = list(self._weights.values())
        return {
            "total_factors": len(weights),
            "avg_weight": round(sum(weights) / len(weights), 4),
            "max_weight": round(max(weights), 4),
            "min_weight": round(min(weights), 4),
            "weights": dict(sorted(self._weights.items(), key=lambda x: x[1], reverse=True)),
        }
