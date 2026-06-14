from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any


Number = float | int
Row = Mapping[str, Any]


def _as_float_series(values: Iterable[Number]) -> list[float]:
    return [float(value) for value in values]


def sma(values: Sequence[Number], period: int) -> list[float | None]:
    _validate_period(period)
    prices = _as_float_series(values)
    result: list[float | None] = [None] * len(prices)
    if len(prices) < period:
        return result

    running_total = sum(prices[:period])
    result[period - 1] = running_total / period

    for index in range(period, len(prices)):
        running_total += prices[index] - prices[index - period]
        result[index] = running_total / period

    return result


def ema(values: Sequence[Number], period: int) -> list[float | None]:
    _validate_period(period)
    prices = _as_float_series(values)
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


def rsi(values: Sequence[Number], period: int = 14) -> list[float | None]:
    _validate_period(period)
    prices = _as_float_series(values)
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


def add_sma_column(
    rows: Sequence[Row],
    *,
    price_key: str = "close",
    period: int = 14,
    column_name: str | None = None,
) -> list[dict[str, Any]]:
    return _add_indicator_column(rows, sma(_extract_prices(rows, price_key), period), column_name or f"sma_{period}")


def add_ema_column(
    rows: Sequence[Row],
    *,
    price_key: str = "close",
    period: int = 14,
    column_name: str | None = None,
) -> list[dict[str, Any]]:
    return _add_indicator_column(rows, ema(_extract_prices(rows, price_key), period), column_name or f"ema_{period}")


def add_rsi_column(
    rows: Sequence[Row],
    *,
    price_key: str = "close",
    period: int = 14,
    column_name: str | None = None,
) -> list[dict[str, Any]]:
    return _add_indicator_column(rows, rsi(_extract_prices(rows, price_key), period), column_name or f"rsi_{period}")


def _add_indicator_column(rows: Sequence[Row], values: Sequence[float | None], column_name: str) -> list[dict[str, Any]]:
    if len(rows) != len(values):
        raise ValueError("indicator output must match the input row count")

    updated_rows: list[dict[str, Any]] = []
    for row, value in zip(rows, values, strict=True):
        new_row = dict(row)
        new_row[column_name] = value
        updated_rows.append(new_row)
    return updated_rows


def _extract_prices(rows: Sequence[Row], price_key: str) -> list[float]:
    try:
        return [float(row[price_key]) for row in rows]
    except KeyError as exc:  # pragma: no cover - simple validation path
        raise KeyError(f"missing required price key: {price_key}") from exc


def _validate_period(period: int) -> None:
    if period <= 0:
        raise ValueError("period must be positive")


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
