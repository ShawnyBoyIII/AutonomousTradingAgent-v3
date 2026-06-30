"""Tests for backtest diagnostics and metrics modules."""

from __future__ import annotations

import pytest

from trading_bot.backtest.diagnostics import attach_diagnostics, diagnostics
from trading_bot.backtest.metrics import compute_win_rate


class TestDiagnostics:
    def test_basic_diagnostics(self):
        result = diagnostics(
            trades=10,
            wins=6,
            losses=4,
            net_pnl=2000.0,
            gross_profit=3000.0,
            gross_loss=-1000.0,
        )
        assert result["avg_win"] == 500.0
        assert result["avg_loss"] == -250.0
        assert result["expectancy"] == 200.0
        assert result["pnl_per_trade"] == 200.0
        assert result["profit_factor"] == 3.0
        assert result["gross_profit"] == 3000.0
        assert result["gross_loss"] == -1000.0

    def test_zero_wins(self):
        result = diagnostics(
            trades=10,
            wins=0,
            losses=10,
            net_pnl=-5000.0,
            gross_profit=0.0,
            gross_loss=-5000.0,
        )
        assert result["avg_win"] == 0.0
        assert result["avg_loss"] == -500.0
        assert result["profit_factor"] == 0.0

    def test_zero_losses(self):
        result = diagnostics(
            trades=10,
            wins=10,
            losses=0,
            net_pnl=5000.0,
            gross_profit=5000.0,
            gross_loss=0.0,
        )
        assert result["avg_win"] == 500.0
        assert result["avg_loss"] == 0.0
        assert result["profit_factor"] == 5000.0

    def test_zero_trades(self):
        result = diagnostics(
            trades=0,
            wins=0,
            losses=0,
            net_pnl=0.0,
        )
        assert result["avg_win"] == 0.0
        assert result["avg_loss"] == 0.0
        assert result["expectancy"] == 0.0
        assert result["pnl_per_trade"] == 0.0
        assert result["profit_factor"] == 0.0

    def test_none_values_handled(self):
        # diagnostics() does not handle None — it raises TypeError
        with pytest.raises(TypeError):
            diagnostics(
                trades=None,
                wins=None,
                losses=None,
                net_pnl=None,
                gross_profit=None,
                gross_loss=None,
            )


class TestAttachDiagnostics:
    def test_attach_diagnostics(self):
        result = {
            "trades": 10,
            "wins": 6,
            "losses": 4,
            "net_pnl": 2000.0,
            "gross_profit": 3000.0,
            "gross_loss": -1000.0,
        }
        result = attach_diagnostics(result)
        assert result["avg_win"] == 500.0
        assert result["avg_loss"] == -250.0
        assert result["profit_factor"] == 3.0

    def test_attach_diagnostics_missing_fields(self):
        result = {}
        result = attach_diagnostics(result)
        assert result["avg_win"] == 0.0
        assert result["expectancy"] == 0.0

    def test_attach_diagnostics_preserves_existing_keys(self):
        result = {"custom_key": "custom_value", "trades": 5, "wins": 3, "losses": 2, "net_pnl": 1000.0}
        result = attach_diagnostics(result)
        assert result["custom_key"] == "custom_value"
        # gross_profit defaults to 0.0 when not provided, so avg_win = 0.0
        assert result["avg_win"] == 0.0
        assert result["avg_loss"] == 0.0
        assert result["expectancy"] == 200.0


class TestComputeWinRate:
    def test_basic_win_rate(self):
        assert compute_win_rate(7, 3) == 0.7

    def test_all_wins(self):
        assert compute_win_rate(10, 0) == 1.0

    def test_all_losses(self):
        assert compute_win_rate(0, 10) == 0.0

    def test_zero_total(self):
        assert compute_win_rate(0, 0) == 0.0

    def test_half_wins(self):
        assert compute_win_rate(5, 5) == 0.5
