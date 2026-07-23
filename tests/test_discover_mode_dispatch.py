"""Regression test for discover --mode dispatch.

The burner runs `discover --mode breakout` so the mode option must
actually route to a different screener. Previously the mode was only
printed and the generic DynamicWatchlist.update path was always used,
making the --mode option a no-op. After the fix, the screeners
screen_for_breakout_setups, screen_for_mean_reversion, and
quick_update_gappers are wired in.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trading_bot.cli.app import app


def _make_frame(close: float = 100.0, high: float = 105.0) -> pd.DataFrame:
    """Tiny frame with enough rows for screener preconditions."""
    dates = pd.date_range("2026-01-01", periods=40, freq="D")
    return pd.DataFrame(
        {
            "open": [close] * 40,
            "high": [high] * 40,
            "low": [close - 1] * 40,
            "close": [close] * 40,
            "volume": [1_000_000] * 40,
        },
        index=dates,
    )


def _build_config(tmp_path: Path, universe_symbols: list[str]) -> Path:
    db = tmp_path / "state.db"
    universe_path = tmp_path / "universe.txt"
    universe_path.write_text("\n".join(universe_symbols), encoding="utf-8")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "app:\n"
        f"  state_db_path: {db}\n"
        f"  universe_path: {universe_path}\n"
        "  scan_results_path: state/scan_results.json\n",
        encoding="utf-8",
    )
    return config_file


def test_discover_breakout_dispatches_to_breakout_screener(tmp_path: Path) -> None:
    """When --mode breakout is set, the screen_for_breakout_setups
    helper must be called instead of the generic DynamicWatchlist path.
    """
    from typer.testing import CliRunner

    config_file = _build_config(tmp_path, ["AAA", "BBB"])
    from trading_bot.strategy import market_screener as screener_module

    breakout_called = MagicMock(return_value=[])

    with patch.object(screener_module, "screen_for_breakout_setups", breakout_called), \
         patch("trading_bot.data.market_data.fetch_bars", return_value=_make_frame()):
        result = CliRunner().invoke(
            app,
            ["--config-path", str(config_file), "discover", "--mode", "breakout"],
        )
    assert result.exit_code == 0, result.output
    assert breakout_called.called, "breakout screener was not invoked"


def test_discover_mean_reversion_dispatches_to_mr_screener(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    config_file = _build_config(tmp_path, ["AAA", "BBB"])
    from trading_bot.strategy import market_screener as screener_module

    mr_called = MagicMock(return_value=[])

    with patch.object(screener_module, "screen_for_mean_reversion", mr_called), \
         patch("trading_bot.data.market_data.fetch_bars", return_value=_make_frame()):
        result = CliRunner().invoke(
            app,
            ["--config-path", str(config_file), "discover", "--mode", "mean-reversion"],
        )
    assert result.exit_code == 0, result.output
    assert mr_called.called, "mean-reversion screener was not invoked"


def test_discover_gap_up_dispatches_to_gapper_helper(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    config_file = _build_config(tmp_path, ["AAA", "BBB"])
    from trading_bot.strategy import dynamic_watchlist as watchlist_module

    gap_called = MagicMock(return_value=[])

    with patch.object(watchlist_module.DynamicWatchlist, "quick_update_gappers", gap_called), \
         patch("trading_bot.data.market_data.fetch_bars", return_value=_make_frame()):
        result = CliRunner().invoke(
            app,
            ["--config-path", str(config_file), "discover", "--mode", "gap-up"],
        )
    assert result.exit_code == 0, result.output
    assert gap_called.called, "gap-up helper was not invoked"
