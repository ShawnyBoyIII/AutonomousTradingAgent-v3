"""Order splitting for large positions.

Splits large orders into smaller chunks to minimize market impact
and improve fill quality. Based on OpenAlgo's split_order_service.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading_bot.execution.paper_broker import PaperBroker
    from trading_bot.portfolio.ledger import PortfolioLedger

logger = logging.getLogger(__name__)

MAX_SPLIT_ORDERS = 10  # Maximum number of split orders


@dataclass
class SplitResult:
    """Result of order splitting operation."""

    total_quantity: int
    split_size: int
    num_orders: int
    successful_orders: int
    failed_orders: int
    results: list[dict]


def calculate_split_size(
    total_quantity: int,
    max_chunk_pct: float = 0.25,
    max_orders: int = MAX_SPLIT_ORDERS,
) -> int:
    """Calculate optimal split size for large order.

    Args:
        total_quantity: Total quantity to split
        max_chunk_pct: Maximum percentage of total per chunk (0.25 = 25%)
        max_orders: Maximum number of split orders allowed

    Returns:
        Optimal split size per order (or total_quantity if no splitting needed)
    """
    if total_quantity <= 0:
        return 0

    # Don't split very small orders (less than 10 shares)
    if total_quantity < 10:
        return total_quantity

    # Calculate minimum number of orders needed
    max_chunk_size = int(total_quantity * max_chunk_pct)
    if max_chunk_size <= 0:
        max_chunk_size = 1

    min_orders_needed = (total_quantity + max_chunk_size - 1) // max_chunk_size

    # If we can do it in 1 order, don't split
    if min_orders_needed <= 1:
        return total_quantity

    # If we need more orders than allowed, increase chunk size
    if min_orders_needed > max_orders:
        split_size = (total_quantity + max_orders - 1) // max_orders
    else:
        split_size = max_chunk_size

    return max(1, split_size)


def split_order(
    symbol: str,
    quantity: int,
    side: str,
    split_size: int | None = None,
    max_chunk_pct: float = 0.25,
    max_orders: int = MAX_SPLIT_ORDERS,
) -> SplitResult:
    """Split a large order into smaller chunks.

    Args:
        symbol: Ticker symbol
        quantity: Total quantity to split
        side: 'buy' or 'sell'
        split_size: Optional fixed split size (calculated if not provided)
        max_chunk_pct: Maximum percentage per chunk if split_size not provided
        max_orders: Maximum number of split orders

    Returns:
        SplitResult with order details
    """
    if quantity <= 0:
        return SplitResult(
            total_quantity=0,
            split_size=0,
            num_orders=0,
            successful_orders=0,
            failed_orders=0,
            results=[],
        )

    # Calculate split size if not provided
    if split_size is None:
        split_size = calculate_split_size(quantity, max_chunk_pct, max_orders)

    # If split size >= quantity, no splitting needed
    if split_size >= quantity:
        return SplitResult(
            total_quantity=quantity,
            split_size=quantity,
            num_orders=1,
            successful_orders=1,
            failed_orders=0,
            results=[
                {
                    "order_num": 1,
                    "quantity": quantity,
                    "status": "success",
                    "note": "No splitting needed",
                }
            ],
        )

    # Calculate number of full-size orders and remainder
    num_full_orders = quantity // split_size
    remainder = quantity % split_size
    total_orders = num_full_orders + (1 if remainder > 0 else 0)

    # Check if exceeds maximum
    if total_orders > max_orders:
        logger.warning(
            f"Order split would create {total_orders} orders, "
            f"exceeding max of {max_orders}. Increasing chunk size."
        )
        split_size = (quantity + max_orders - 1) // max_orders
        num_full_orders = quantity // split_size
        remainder = quantity % split_size
        total_orders = num_full_orders + (1 if remainder > 0 else 0)

    results = []

    # Create full-size orders
    for i in range(num_full_orders):
        results.append(
            {
                "order_num": i + 1,
                "quantity": split_size,
                "status": "pending",
            }
        )

    # Create remainder order if any
    if remainder > 0:
        results.append(
            {
                "order_num": total_orders,
                "quantity": remainder,
                "status": "pending",
            }
        )

    logger.info(
        f"Split order for {symbol}: {quantity} -> {total_orders} orders "
        f"(size={split_size}, remainder={remainder})"
    )

    return SplitResult(
        total_quantity=quantity,
        split_size=split_size,
        num_orders=total_orders,
        successful_orders=0,  # Will be updated after execution
        failed_orders=0,
        results=results,
    )


async def execute_split_orders(
    split_result: SplitResult,
    symbol: str,
    side: str,
    broker: PaperBroker,
    ledger: PortfolioLedger,
    order_type: str = "MARKET",
    delay_between_orders: float = 0.1,
) -> SplitResult:
    """Execute split orders sequentially with rate limiting.

    Args:
        split_result: SplitResult from split_order()
        symbol: Ticker symbol
        side: 'buy' or 'sell'
        broker: PaperBroker instance
        ledger: PortfolioLedger instance
        order_type: Order type (MARKET, LIMIT, etc.)
        delay_between_orders: Delay between orders in seconds

    Returns:
        Updated SplitResult with execution results
    """
    raise NotImplementedError(
        "execute_split_orders is not wired into the broker order flow yet"
    )
