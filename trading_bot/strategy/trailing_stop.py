from __future__ import annotations

from trading_bot.models.portfolio import Position


def ratchet_stop(
    current_stop: float | None,
    entry_price: float,
    last_price: float,
    initial_risk: float,
) -> float | None:
    """R-multiple ratchet, never trails down.

    Once `last_price` reaches `entry_price + initial_risk` (i.e. +1R of
    profit), the candidate stop becomes `last_price - initial_risk` so
    trades ratchet their stop one full R below the current price:
      - at +1R, candidate stops at breakeven (entry_price)
      - at +1.5R, candidate stops at +0.5R
      - at +2R, candidate stops at +1R
    Returns the candidate only when it strictly exceeds `current_stop`.
    """
    if initial_risk <= 0:
        return current_stop

    candidate = last_price - initial_risk
    if candidate < entry_price:
        return current_stop

    if current_stop is None:
        return round(candidate, 4)
    if candidate <= current_stop:
        return current_stop
    return round(candidate, 4)


def chandelier_stop(
    highest_high: float,
    atr: float | None,
    multiplier: float = 1.5,
) -> float | None:
    """Chandelier exit: `highest_high - (multiplier * atr)`.

    Returns None when ATR is missing or non-positive so callers can skip
    the chandelier contribution without special-casing.
    """
    if atr is None or atr <= 0:
        return None
    return round(highest_high - (multiplier * atr), 4)


def next_trailing_stop(
    position: Position,
    last_price: float,
    atr: float | None,
) -> tuple[float | None, str | None]:
    """Combine R-multiple ratchet and chandelier ATR trails.

    Returns `(new_stop, method)` where `new_stop` is strictly higher
    than the position's current `stop_loss`. Methods are reported as
    `r-multiple`, `chandelier-atr`, or `both` (when both candidates
    agree on the same level). Returns `(None, None)` when no
    tightening is warranted this run.
    """
    current_stop = position.stop_loss
    initial_risk = position.initial_risk
    highest_high = position.highest_high

    ratchet_candidate: float | None = None
    if initial_risk is not None and initial_risk > 0:
        ratcheted = ratchet_stop(
            current_stop,
            position.average_cost,
            last_price,
            initial_risk,
        )
        if ratcheted is not None and (current_stop is None or ratcheted > current_stop):
            ratchet_candidate = ratcheted

    chandelier_candidate: float | None = None
    if highest_high is not None:
        chand = chandelier_stop(highest_high, atr)
        if chand is not None and (current_stop is None or chand > current_stop):
            chandelier_candidate = chand

    if ratchet_candidate is None and chandelier_candidate is None:
        return None, None

    if ratchet_candidate is not None and chandelier_candidate is not None:
        if chandelier_candidate > ratchet_candidate:
            return chandelier_candidate, "chandelier-atr"
        if ratchet_candidate > chandelier_candidate:
            return ratchet_candidate, "r-multiple"
        return ratchet_candidate, "both"

    if ratchet_candidate is not None:
        return ratchet_candidate, "r-multiple"
    return chandelier_candidate, "chandelier-atr"
