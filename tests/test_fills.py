"""Tests for execution.fills module (3 lines)."""

from __future__ import annotations

import pytest

from trading_bot.execution.fills import apply_slippage


class TestApplySlippage:
    def test_buy_slippage_increases_price(self) -> None:
        assert apply_slippage(100.0, slippage_bps=10, side="BUY") == pytest.approx(100.1)

    def test_sell_slippage_decreases_price(self) -> None:
        assert apply_slippage(100.0, slippage_bps=10, side="SELL") == pytest.approx(99.9)

    def test_zero_bps_returns_price_unchanged(self) -> None:
        assert apply_slippage(50.0, slippage_bps=0, side="BUY") == 50.0
        assert apply_slippage(50.0, slippage_bps=0, side="SELL") == 50.0

    def test_large_bps(self) -> None:
        # 100 bps = 1.0%
        assert apply_slippage(100.0, slippage_bps=100, side="BUY") == pytest.approx(101.0)
        assert apply_slippage(100.0, slippage_bps=100, side="SELL") == pytest.approx(99.0)

    def test_unknown_side_treats_as_sell_direction(self) -> None:
        # side != "BUY" => direction = -1
        assert apply_slippage(100.0, slippage_bps=10, side="HOLD") == pytest.approx(99.9)

    def test_negative_price_passthrough(self) -> None:
        # Math is direction-based; verify it still applies
        assert apply_slippage(-100.0, slippage_bps=10, side="BUY") == pytest.approx(-100.1)