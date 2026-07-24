"""Regression test for discovery failure visibility.

Audit follow-up (2026-07-24): when the screener returns 0 candidates,
the discover CLI silently preserves the existing universe. The
"Exported N symbols" line is misleading because N is the count of
the preserved (not newly-discovered) symbols. An operator running
the burner can't tell that discovery failed.

Two contracts to enforce:

1. The CLI must print a clear "0 candidates" line when no candidates
   pass screening.
2. When --export is set and 0 candidates passed, the CLI must exit
   with a non-zero status so the burner shell can detect the failure.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from typer.testing import CliRunner

from trading_bot.cli.app import app


def _make_frame(close: float = 100.0, high: float = 105.0) -> pd.DataFrame:
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


def test_discover_emits_zero_candidate_warning(tmp_path: Path) -> None:
    """When the screener returns zero results, the CLI must print a
    clear, machine-greppable warning so an operator can detect the
    silent-failure mode without reading log files.
    """
    from trading_bot.strategy import market_screener as screener_module

    config_file = _build_config(tmp_path, ["AAA", "BBB"])
    breakout_called = MagicMock(return_value=[])

    with patch.object(screener_module, "screen_for_breakout_setups", breakout_called), \
         patch("trading_bot.data.market_data.fetch_bars", return_value=_make_frame()):
        result = CliRunner().invoke(
            app,
            ["--config-path", str(config_file), "discover", "--mode", "breakout", "--export"],
        )

    assert "0 candidates" in result.output, (
        f"expected '0 candidates' warning in stdout, got: {result.output!r}"
    )


def test_discover_exits_nonzero_when_no_candidates_with_export(tmp_path: Path) -> None:
    """When --export is set and the screener returns 0 candidates,
    the CLI must exit non-zero so the burner shell can detect the
    failure (currently it runs `if echo $output | grep -q 'Exported'`
    which lies because 'Exported N' prints even when N=preserved).
    """
    from trading_bot.strategy import market_screener as screener_module

    config_file = _build_config(tmp_path, ["AAA", "BBB"])
    breakout_called = MagicMock(return_value=[])

    with patch.object(screener_module, "screen_for_breakout_setups", breakout_called), \
         patch("trading_bot.data.market_data.fetch_bars", return_value=_make_frame()):
        result = CliRunner().invoke(
            app,
            ["--config-path", str(config_file), "discover", "--mode", "breakout", "--export"],
        )

    assert result.exit_code != 0, (
        f"expected non-zero exit code on discovery failure, got {result.exit_code}; "
        f"output: {result.output!r}"
    )


def test_discover_succeeds_when_candidates_found(tmp_path: Path) -> None:
    """Sanity check: when candidates ARE found, the CLI exits 0
    and does not emit the '0 candidates' warning.
    """
    from trading_bot.strategy import market_screener as screener_module

    config_file = _build_config(tmp_path, ["AAA", "BBB"])
    found = [
        screener_module.ScreenResult(symbol="AAA", passed=True, score=70.0,
                                     reasons=["Near 20d high"], metrics={})
    ]
    breakout_called = MagicMock(return_value=found)

    with patch.object(screener_module, "screen_for_breakout_setups", breakout_called), \
         patch("trading_bot.data.market_data.fetch_bars", return_value=_make_frame()):
        result = CliRunner().invoke(
            app,
            ["--config-path", str(config_file), "discover", "--mode", "breakout", "--export"],
        )

    assert result.exit_code == 0, result.output
    assert "0 candidates" not in result.output