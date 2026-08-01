"""Helpers for building a resilient paper-trading universe."""

from __future__ import annotations

from collections.abc import Iterable


def _normalise_symbols(symbols: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def merge_universe_symbols(
    static_symbols: Iterable[str],
    watchlist_symbols: Iterable[str],
    scout_symbols: Iterable[str],
    previous_symbols: Iterable[str],
    *,
    max_size: int,
    min_size: int,
) -> tuple[list[str], bool]:
    """Merge symbol sources while preserving a healthy prior universe.

    Static core and operator watchlist symbols are always prioritised. The
    previous universe is appended when the new sources do not meet the
    configured minimum, so a transient provider failure cannot erase coverage.
    """
    if max_size < 1:
        return [], False

    static = _normalise_symbols(static_symbols)
    watchlist = _normalise_symbols(watchlist_symbols)
    scout = _normalise_symbols(scout_symbols)
    previous = _normalise_symbols(previous_symbols)

    fresh = _normalise_symbols((*static, *watchlist, *scout))
    preserved = len(fresh) < max(1, min_size) and bool(previous)
    merged = _normalise_symbols((*fresh, *previous))
    return merged[:max_size], preserved
