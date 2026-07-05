"""Tests for order splitting functionality."""

from __future__ import annotations

import pytest

from trading_bot.execution.order_splitter import (
    SplitResult,
    calculate_split_size,
    execute_split_orders,
    split_order,
)


class TestCalculateSplitSize:
    """Tests for calculate_split_size function."""

    def test_small_quantity_splits_if_needed(self) -> None:
        # 10 shares with 25% max chunk = 2.5 -> 2 per order
        # Would need 5 orders (10/2), which is < max_orders (10)
        result = calculate_split_size(10, max_chunk_pct=0.25, max_orders=10)
        assert result == 2  # 10 * 0.25 = 2.5, rounded down to 2

    def test_very_small_quantity_returns_full_amount(self) -> None:
        # Orders < 10 shares should not be split
        result = calculate_split_size(3, max_chunk_pct=0.25, max_orders=10)
        assert result == 3  # No splitting for very small orders

    def test_minimum_split_threshold(self) -> None:
        # 9 shares should not split, but 10 should
        assert calculate_split_size(9, 0.25, 10) == 9
        assert calculate_split_size(10, 0.25, 10) == 2  # Would split into 5 orders

    def test_split_when_exceeds_chunk_size(self) -> None:
        # 100 shares with 25% max chunk = 25 per order
        result = calculate_split_size(100, max_chunk_pct=0.25, max_orders=10)
        assert result == 25

    def test_respects_max_orders_limit(self) -> None:
        # 100 shares with 10% max chunk would need 10 orders
        # But if max_orders is 5, should increase chunk size
        result = calculate_split_size(100, max_chunk_pct=0.10, max_orders=5)
        assert result == 20  # 100 / 5 = 20

    def test_zero_quantity_returns_zero(self) -> None:
        result = calculate_split_size(0)
        assert result == 0

    def test_negative_quantity_returns_zero(self) -> None:
        result = calculate_split_size(-10)
        assert result == 0

    def test_minimum_split_size_is_one(self) -> None:
        # Even with very small percentage, should return at least 1
        result = calculate_split_size(10, max_chunk_pct=0.01, max_orders=10)
        assert result >= 1


class TestSplitOrder:
    """Tests for split_order function."""

    def test_no_split_for_single_order(self) -> None:
        result = split_order("AAPL", quantity=10, side="buy", split_size=25)
        assert result.num_orders == 1
        assert result.total_quantity == 10
        assert result.split_size == 10
        assert len(result.results) == 1
        assert result.results[0]["quantity"] == 10

    def test_splits_into_multiple_orders(self) -> None:
        result = split_order("AAPL", quantity=100, side="buy", split_size=25)
        assert result.num_orders == 4
        assert result.total_quantity == 100
        assert result.split_size == 25
        assert len(result.results) == 4

        # Check all quantities
        quantities = [r["quantity"] for r in result.results]
        assert quantities == [25, 25, 25, 25]

    def test_handles_remainder(self) -> None:
        result = split_order("AAPL", quantity=110, side="buy", split_size=25)
        assert result.num_orders == 5
        assert result.total_quantity == 110

        quantities = [r["quantity"] for r in result.results]
        assert quantities == [25, 25, 25, 25, 10]

    def test_respects_max_orders(self) -> None:
        # 100 shares with split_size=5 would need 20 orders
        # But max_orders=10 should limit it
        result = split_order(
            "AAPL", quantity=100, side="buy", split_size=5, max_orders=10
        )
        assert result.num_orders <= 10
        assert result.total_quantity == 100

    def test_zero_quantity_returns_empty_result(self) -> None:
        result = split_order("AAPL", quantity=0, side="buy")
        assert result.num_orders == 0
        assert result.total_quantity == 0
        assert len(result.results) == 0

    def test_negative_quantity_returns_empty_result(self) -> None:
        result = split_order("AAPL", quantity=-10, side="buy")
        assert result.num_orders == 0
        assert result.total_quantity == 0

    def test_result_has_correct_structure(self) -> None:
        result = split_order("AAPL", quantity=50, side="sell", split_size=25)
        assert result.successful_orders == 0  # Not executed yet
        assert result.failed_orders == 0

        for order_info in result.results:
            assert "order_num" in order_info
            assert "quantity" in order_info
            assert "status" in order_info
            assert order_info["status"] == "pending"

    def test_order_numbers_are_sequential(self) -> None:
        result = split_order("AAPL", quantity=75, side="buy", split_size=25)
        order_nums = [r["order_num"] for r in result.results]
        assert order_nums == [1, 2, 3]

    def test_sell_side_works_same_as_buy(self) -> None:
        buy_result = split_order("AAPL", quantity=50, side="buy", split_size=25)
        sell_result = split_order("AAPL", quantity=50, side="sell", split_size=25)

        assert buy_result.num_orders == sell_result.num_orders
        assert buy_result.total_quantity == sell_result.total_quantity


class TestSplitResult:
    """Tests for SplitResult dataclass."""

    def test_split_result_creation(self) -> None:
        result = SplitResult(
            total_quantity=100,
            split_size=25,
            num_orders=4,
            successful_orders=0,
            failed_orders=0,
            results=[],
        )

        assert result.total_quantity == 100
        assert result.split_size == 25
        assert result.num_orders == 4
        assert result.successful_orders == 0
        assert result.failed_orders == 0

    def test_split_result_with_results(self) -> None:
        results = [
            {"order_num": 1, "quantity": 25, "status": "pending"},
            {"order_num": 2, "quantity": 25, "status": "pending"},
        ]

        split_result = SplitResult(
            total_quantity=50,
            split_size=25,
            num_orders=2,
            successful_orders=0,
            failed_orders=0,
            results=results,
        )

        assert len(split_result.results) == 2
        assert split_result.results[0]["order_num"] == 1


class TestExecuteSplitOrders:
    def test_raises_until_broker_flow_is_wired(self) -> None:
        import asyncio

        split_result = split_order("AAPL", quantity=50, side="buy", split_size=25)

        with pytest.raises(NotImplementedError, match="not wired"):
            asyncio.run(
                execute_split_orders(
                    split_result,
                    symbol="AAPL",
                    side="buy",
                    broker=None,
                    ledger=None,
                )
            )
