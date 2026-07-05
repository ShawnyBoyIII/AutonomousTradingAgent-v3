from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def calculate_atr_position_size(
    account_equity: float,
    risk_pct: float,
    entry_price: float,
    atr: float,
    atr_multiplier: float = 2.0,
    max_position_pct: float = 0.20,
) -> int:
    """Calculate position size based on ATR (Average True Range) volatility.

    Higher volatility (larger ATR) = smaller position size
    Lower volatility (smaller ATR) = larger position size

    Args:
        account_equity: Total account value
        risk_pct: Percentage of account to risk per trade (e.g., 0.01 for 1%)
        entry_price: Entry price for the trade
        atr: 14-period Average True Range
        atr_multiplier: Multiplier for ATR to set effective stop distance
        max_position_pct: Maximum position size as % of equity (default 20%)

    Returns:
        Number of shares to trade (0 if calculation invalid)

    Example:
        $100k equity, 1% risk = $1k risk
        Entry $100, ATR = $5, multiplier = 2 → stop distance = $10
        Position size = $1k / $10 = 100 shares ($10k position = 10% of equity)
    """
    equity = Decimal(str(account_equity))
    percentage = Decimal(str(risk_pct))
    entry = Decimal(str(entry_price))
    atr_dec = Decimal(str(atr))
    multiplier = Decimal(str(atr_multiplier))
    max_pct = Decimal(str(max_position_pct))

    if equity <= 0 or percentage <= 0 or atr_dec <= 0 or entry <= 0:
        return 0

    # Calculate effective stop distance based on volatility
    stop_distance = atr_dec * multiplier
    if stop_distance <= 0:
        return 0

    # Calculate dollar risk amount
    dollar_risk = equity * percentage

    # Calculate position size: risk_amount / stop_distance
    shares = (dollar_risk / stop_distance).to_integral_value(rounding=ROUND_FLOOR)

    # Apply maximum position size constraint
    max_position_value = equity * max_pct
    max_shares = (max_position_value / entry).to_integral_value(rounding=ROUND_FLOOR)

    # Return the smaller of risk-based size and max position size
    return int(min(shares, max_shares))


def calculate_fixed_stop_position_size(
    account_equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    max_position_pct: float = 0.20,
) -> int:
    """Original position sizing based on fixed stop loss distance.

    Args:
        account_equity: Total account value
        risk_pct: Percentage of account to risk per trade
        entry_price: Entry price for the trade
        stop_loss: Stop loss price
        max_position_pct: Maximum position size as % of equity

    Returns:
        Number of shares to trade
    """
    equity = Decimal(str(account_equity))
    percentage = Decimal(str(risk_pct))
    entry = Decimal(str(entry_price))
    stop = Decimal(str(stop_loss))
    max_pct = Decimal(str(max_position_pct))

    risk_per_share = entry - stop
    if equity <= 0 or percentage <= 0 or risk_per_share <= 0:
        return 0

    dollar_risk = equity * percentage
    shares = (dollar_risk / risk_per_share).to_integral_value(rounding=ROUND_FLOOR)

    # Apply maximum position size constraint
    max_position_value = equity * max_pct
    max_shares = (max_position_value / entry).to_integral_value(rounding=ROUND_FLOOR)

    return int(min(shares, max_shares))


def calculate_kelly_fraction(
    win_probability: float,
    reward_risk_ratio: float,
) -> float:
    """Return the raw Kelly fraction for a trade edge.

    Uses ``f* = p - (1 - p) / b`` where ``p`` is win probability and
    ``b`` is reward/risk. Invalid or non-positive edges return ``0.0``.
    """
    if reward_risk_ratio <= 0:
        return 0.0

    p = max(0.0, min(1.0, float(win_probability)))
    b = float(reward_risk_ratio)
    q = 1.0 - p
    return max(0.0, p - (q / b))


def apply_fractional_kelly(
    position_size: int,
    win_probability: float,
    reward_risk_ratio: float,
    scale: float = 0.5,
    min_position_pct: float = 0.25,
) -> tuple[int, float, float]:
    """Scale a baseline position size by a fractional Kelly overlay.

    Returns ``(scaled_position_size, multiplier, raw_kelly_fraction)``.
    A non-positive Kelly edge returns ``(0, 0.0, 0.0)``.
    """
    if position_size <= 0:
        return 0, 0.0, 0.0

    raw_kelly = calculate_kelly_fraction(win_probability, reward_risk_ratio)
    if raw_kelly <= 0:
        return 0, 0.0, 0.0

    multiplier = max(min_position_pct, min(1.0, raw_kelly * scale))
    scaled_size = max(1, int(position_size * multiplier))
    return scaled_size, multiplier, raw_kelly
