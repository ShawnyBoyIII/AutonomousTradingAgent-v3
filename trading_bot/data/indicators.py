from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def add_ema(frame: "pd.DataFrame", period: int, column_name: str) -> "pd.DataFrame":
    prices = _close_prices(frame)
    values = _ema_values(prices, period)
    return _with_column(frame, column_name, values)


def add_rsi(frame: "pd.DataFrame", period: int = 14) -> "pd.DataFrame":
    prices = _close_prices(frame)
    values = _rsi_values(prices, period)
    return _with_column(frame, f"rsi_{period}", values)


def add_sma(frame: "pd.DataFrame", period: int = 14, column_name: str | None = None) -> "pd.DataFrame":
    prices = _close_prices(frame)
    values = _sma_values(prices, period)
    return _with_column(frame, column_name or f"sma_{period}", values)


def _close_prices(frame: "pd.DataFrame") -> list[float]:
    if "close" not in frame.columns:
        raise KeyError("missing required price column: close")
    return [float(value) for value in frame["close"].tolist()]


def _with_column(frame: "pd.DataFrame", column_name: str, values: list[float | None]) -> "pd.DataFrame":
    if len(frame.index) != len(values):
        raise ValueError("indicator output must match input rows")

    result = frame.copy(deep=True)
    result[column_name] = values
    return result


def _validate_period(period: int) -> None:
    if period <= 0:
        raise ValueError("period must be positive")


def _sma_values(prices: list[float], period: int) -> list[float | None]:
    _validate_period(period)
    result: list[float | None] = [None] * len(prices)
    if len(prices) < period:
        return result

    running_total = sum(prices[:period])
    result[period - 1] = running_total / period

    for index in range(period, len(prices)):
        running_total += prices[index] - prices[index - period]
        result[index] = running_total / period

    return result


def _ema_values(prices: list[float], period: int) -> list[float | None]:
    _validate_period(period)
    result: list[float | None] = [None] * len(prices)
    if len(prices) < period:
        return result

    multiplier = 2.0 / (period + 1.0)
    current = sum(prices[:period]) / period
    result[period - 1] = current

    for index in range(period, len(prices)):
        current = (prices[index] - current) * multiplier + current
        result[index] = current

    return result


def _rsi_values(prices: list[float], period: int) -> list[float | None]:
    _validate_period(period)
    result: list[float | None] = [None] * len(prices)
    if len(prices) <= period:
        return result

    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(prices)):
        change = prices[index] - prices[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_from_averages(avg_gain, avg_loss)

    for index in range(period + 1, len(prices)):
        gain = gains[index - 1]
        loss = losses[index - 1]
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        result[index] = _rsi_from_averages(avg_gain, avg_loss)

    return result


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0

    relative_strength = avg_gain / avg_loss
    value = 100.0 - (100.0 / (1.0 + relative_strength))
    return max(0.0, min(100.0, value))
