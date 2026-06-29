"""Tests for alpha zoo benching cron job."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestAutoBenchCron:
    """Tests for auto_bench_cron.py."""

    @pytest.fixture
    def mock_frame(self):
        """Create a mock OHLCV frame for testing."""
        import pandas as pd

        return pd.DataFrame({
            "timestamp": pd.date_range("2024-01-01", periods=300, freq="D"),
            "open": [100.0 + i * 0.1 for i in range(300)],
            "high": [100.5 + i * 0.1 for i in range(300)],
            "low": [99.5 + i * 0.1 for i in range(300)],
            "close": [100.0 + i * 0.1 for i in range(300)],
            "volume": [1_000_000 for _ in range(300)],
        })

    def test_run_benching_with_mock_data(self, mock_frame, tmp_path):
        """Test benching with mock market data."""
        import pandas as pd

        # Mock fetch_bars to return our mock frame
        with patch("trading_bot.data.market_data.fetch_bars") as mock_fetch:
            mock_fetch.return_value = mock_frame

            from scripts.auto_bench_cron import run_benching

            output_path = str(tmp_path / "bench_results.json")
            results = run_benching(
                symbols=["SPY"],
                zoo="qlib",
                lookback=60,
                min_ic_ir=0.0,
                output_path=output_path,
            )

            # Should have results
            assert results is not None
            assert "aggregate" in results or "factors" in results

            # Results should be saved to file
            assert Path(output_path).exists()
            with open(output_path) as f:
                saved_results = __import__("json").load(f)
            assert saved_results is not None

    def test_run_benching_no_data(self):
        """Test benching with no market data."""
        with patch("trading_bot.data.market_data.fetch_bars") as mock_fetch:
            mock_fetch.return_value = None

            from scripts.auto_bench_cron import run_benching

            results = run_benching(
                symbols=["SPY"],
                zoo="qlib",
                lookback=60,
            )

            assert results == {}

    def test_run_benching_invalid_zoo(self):
        """Test benching with invalid zoo name."""
        with patch("trading_bot.data.market_data.fetch_bars") as mock_fetch:
            mock_fetch.return_value = MagicMock()

            from scripts.auto_bench_cron import run_benching

            results = run_benching(
                symbols=["SPY"],
                zoo="invalid_zoo",
                lookback=60,
            )

            assert results == {}

    def test_run_benching_updates_weights(self, mock_frame, tmp_path):
        """Test that benching updates weights file."""
        import pandas as pd

        with patch("trading_bot.data.market_data.fetch_bars") as mock_fetch:
            mock_fetch.return_value = mock_frame

            weights_path = str(tmp_path / "factor_bench_weights.json")
            with patch("scripts.auto_bench_cron.BenchingWeightsManager") as mock_manager_cls:
                mock_manager = MagicMock()
                mock_manager_cls.return_value = mock_manager

                from scripts.auto_bench_cron import run_benching

                run_benching(
                    symbols=["SPY"],
                    zoo="qlib",
                    lookback=60,
                    min_ic_ir=0.1,
                )

                # Manager should have been called to update weights
                mock_manager.update_from_benching.assert_called_once()
