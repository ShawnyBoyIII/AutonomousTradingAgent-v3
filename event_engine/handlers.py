"""Multi-asset market-data handlers.

Two responsibilities:

* :class:`AbstractDataHandler` defines the contract every concrete
  handler must satisfy: ingest one or more time-series, expose the
  most recent bar for any symbol through :meth:`latest_bar`, advance
  in time through :meth:`stream`, and enforce point-in-time lookup
  via :meth:`get_bar`. The :meth:`stream` method is a generator
  that pushes MarketEvent instances into a caller-supplied
  :class:`EventQueue` in merged chronological order.

* :class:`HistoricCSVDataHandler` is the only concrete handler in
  this module today — it ingests CSV files (one file per symbol)
  and :class:`ParquetDataHandler`-shaped in-memory frames, merges
  them by timestamp, and emits merged ``MarketEvent`` instances. The
  data is held in sorted NumPy ``int64`` arrays so per-symbol
  latest-bar lookups are O(1) and ``stream`` is O(total bars).

A separate :class:`CSVDataSource` dataclass encapsulates a single
symbol's price array plus a tag identifying the bar type — handy
for callers that want to mix 1m ticks with 5m bars in one run.

Complexity:

* Ingest (CSV or in-memory): ``O(N)`` per asset (``N`` = bar count).
* :meth:`latest_bar`: ``O(1)``.
* :meth:`stream`: ``O(total_bars_across_all_symbols)`` since it
  advances one row per asset per yield.
* Memory: 8 bytes per row in the price column × (N+1) per asset.

Both handlers fail closed on malformed rows
(:class:`DataHandlerError`) and on any
:meth:`AbstractDataHandler.get_bar` call whose timestamp is
strictly greater than the simulator's last-consumed wall clock
(:class:`PointInTimeLeakError`).
"""
from __future__ import annotations

import csv
import heapq
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

import numpy as np

from event_engine.events import BarType, Event, MarketEvent
from event_engine.exceptions import (
    DataHandlerError,
    EventValidationError,
    PointInTimeLeakError,
    UnknownSymbolError,
)
from event_engine.queue import EventQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_optional(value: str) -> Optional[float]:
    """Parse ``value`` as a float unless it is empty / ``"NA"`` /
    ``"nan"``-like. Returns ``None`` in that case so callers can fall
    back to a default."""
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"nan", "na", "null", "none"}:
        return None
    try:
        out = float(cleaned)
    except ValueError as exc:
        raise DataHandlerError(f"Cannot parse {value!r} as float") from exc
    return out


# ---------------------------------------------------------------------------
# Abstract handler
# ---------------------------------------------------------------------------


class AbstractDataHandler(ABC):
    """Base class for market-data handlers."""

    @abstractmethod
    def symbols(self) -> tuple[str, ...]:
        """Symbols currently loaded."""

    @abstractmethod
    def latest_bar(self, symbol: str) -> Optional[MarketEvent]:
        """Most recent :class:`MarketEvent` already streamed for
        ``symbol``, or ``None`` if nothing has been streamed yet."""

    @abstractmethod
    def get_bar(self, symbol: str, timestamp_ns: int) -> Optional[MarketEvent]:
        """Return the bar *at or before* ``timestamp_ns`` (point-in-time).

        Raises
        ------
        UnknownSymbolError
            If ``symbol`` is not registered.
        PointInTimeLeakError
            If ``timestamp_ns`` is in the *future* of the simulator's
            last-consumed timestamp.
        """

    @abstractmethod
    def stream(self, queue: EventQueue) -> Iterator[MarketEvent]:
        """Generator that emits MarketEvent instances in merged
        chronological order. Each yielded event is also ``put`` on
        ``queue`` before ``stream`` advances the simulator clock.

        Implementations advance the handler's internal time cursor
        on each iteration; callers can pause/resume mid-stream by
        iterating ``stream`` synchronously and resuming the iterator.
        """

    @abstractmethod
    def reset(self) -> None:
        """Reset per-run state — ``cursor_ns``, per-symbol
        ``latest_emitted`` caches, and any in-flight iteration.
        Called by ``EngineDriver.reset()`` between runs so the same
        data can be replayed cleanly."""


# ---------------------------------------------------------------------------
# CSV / numpy data source
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _AssetSeries:
    """One symbol's price frame held entirely in memory."""

    symbol: str
    timestamps_ns: np.ndarray  # int64, ascending unique
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    volumes: np.ndarray
    bid_ask_spreads: np.ndarray
    bar_type: BarType
    _cursor: int = 0  # private; lives in slots dataclass
    last_emitted_ns: Optional[int] = field(default=None)

    @property
    def exhausted(self) -> bool:
        return self._cursor >= self.timestamps_ns.shape[0]

    def next_index(self) -> int:
        return self._cursor

    def advance(self) -> int:
        """Advance the cursor by one and return the *new* cursor
        position (the index the caller should read next)."""
        self._cursor += 1
        return self._cursor

    def event_at(self, idx: int) -> MarketEvent:
        return MarketEvent(
            timestamp_ns=int(self.timestamps_ns[idx]),
            symbol=self.symbol,
            open=float(self.opens[idx]),
            high=float(self.highs[idx]),
            low=float(self.lows[idx]),
            close=float(self.closes[idx]),
            volume=float(self.volumes[idx]),
            bid_ask_spread=float(self.bid_ask_spreads[idx]),
            bar_type=self.bar_type,
        )


def _series_from_records(
    symbol: str,
    rows: Iterable[tuple[int, float, float, float, float, float, float]],
    bar_type: BarType,
) -> _AssetSeries:
    """Build an in-memory series from pre-parsed rows.

    The iterable yields tuples of
    ``(timestamp_ns, open, high, low, close, volume, bid_ask_spread)``.
    Rows must arrive in strictly ascending timestamp order; the
    handler sorts defensively (small ops cost) before validating.
    """
    timestamps: list[int] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    volumes: list[float] = []
    bid_ask_spreads: list[float] = []
    for ts, o, h, l, c, v, bid_ask in rows:
        timestamps.append(int(ts))
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        volumes.append(v)
        bid_ask_spreads.append(bid_ask)
    if not timestamps:
        raise DataHandlerError(f"data source for {symbol!r} contains no rows")
    order = np.argsort(timestamps, kind="stable")
    timestamps_arr = np.array(timestamps, dtype=np.int64)[order]
    return _AssetSeries(
        symbol=symbol,
        timestamps_ns=timestamps_arr,
        opens=np.array(opens, dtype=np.float64)[order],
        highs=np.array(highs, dtype=np.float64)[order],
        lows=np.array(lows, dtype=np.float64)[order],
        closes=np.array(closes, dtype=np.float64)[order],
        volumes=np.array(volumes, dtype=np.float64)[order],
        bid_ask_spreads=np.array(bid_ask_spreads, dtype=np.float64)[order],
        bar_type=bar_type,
    )


def _series_from_csv(
    symbol: str,
    csv_path: Path,
    bar_type: BarType,
    timestamp_unit: str = "ns",
    timestamp_column: str = "timestamp",
) -> _AssetSeries:
    """Load a CSV file with one column per ``_series_from_records`` arg.

    ``timestamp_unit`` selects how to interpret the timestamp column:

    * ``"ns"`` (default) — integer nanoseconds since the Unix epoch.
    * ``"us"`` — microseconds, multiplied by 1000.
    * ``"ms"`` — milliseconds, multiplied by 1_000_000.
    * ``"s"``  — seconds, multiplied by 1_000_000_000.

    All other columns are coerced to float via :func:`_parse_optional`
    (so empty / NA cells map to NaN, which
    :class:`MarketEvent` will then reject).
    """
    rows: list[tuple[int, float, float, float, float, float, float]] = []

    def _scale(ts: int) -> int:
        if timestamp_unit == "ns":
            return ts
        if timestamp_unit == "us":
            return ts * 1_000
        if timestamp_unit == "ms":
            return ts * 1_000_000
        if timestamp_unit == "s":
            return ts * 1_000_000_000
        raise DataHandlerError(f"Unknown timestamp_unit: {timestamp_unit!r}")

    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for line_no, row in enumerate(reader, start=2):
            try:
                ts = int(row[timestamp_column])
            except KeyError as exc:
                raise DataHandlerError(
                    f"CSV {csv_path} missing column {timestamp_column!r} "
                    f"(line {line_no})"
                ) from exc
            try:
                rows.append(
                    (
                        _scale(ts),
                        _parse_optional(row["open"]),
                        _parse_optional(row["high"]),
                        _parse_optional(row["low"]),
                        _parse_optional(row["close"]),
                        _parse_optional(row["volume"]),
                        _parse_optional(row["bid_ask_spread"]),
                    )
                )
            except DataHandlerError as exc:
                raise DataHandlerError(
                    f"CSV {csv_path} parse error on line {line_no}: {exc}"
                ) from exc
    return _series_from_records(symbol, rows, bar_type)


# ---------------------------------------------------------------------------
# Concrete handler
# ---------------------------------------------------------------------------


class HistoricCSVDataHandler(AbstractDataHandler):
    """Ingest a directory of CSVs (one per symbol) and emit merged
    ``MarketEvent`` instances.

    A naïve CSV is permitted too — call
    :meth:`register_in_memory_series` to inject a synthetic series
    without writing to disk. Multiple registrations for the same
    symbol raise :class:`DataHandlerError`.
    """

    def reset(self) -> None:
        """Reset the simulator cursor so ``stream`` can be replayed.

        Clears :attr:`_cursor_ns` and :attr:`_latest_emitted`, and
        walks every registered series to put its per-symbol cursor
        back to zero. After this call, the next ``stream`` invocation
        pushes every bar from the start.
        """
        self._cursor_ns = None
        self._latest_emitted.clear()
        for series in self._series.values():
            series._cursor = 0

    def __init__(self) -> None:
        self._series: dict[str, _AssetSeries] = {}
        # ``_cursor_ns`` is the simulator's last-advanced timestamp.
        self._cursor_ns: Optional[int] = None
        # ``_latest_emitted`` retains the most recent MarketEvent per
        # symbol so :meth:`latest_bar` and other downstream consumers
        # don't need to re-read the source arrays.
        self._latest_emitted: dict[str, MarketEvent] = {}

    # ------------------------------------------------------------------
    # Ingest API
    # ------------------------------------------------------------------

    def register_in_memory_series(
        self,
        symbol: str,
        rows: Sequence[
            tuple[int, float, float, float, float, float, float]
        ],
        bar_type: BarType = BarType.BAR_1M,
    ) -> None:
        """Inject a synthetic series. ``rows`` is in *any* order;
        the handler sorts defensively."""
        if symbol in self._series:
            raise DataHandlerError(
                f"symbol {symbol!r} already registered; "
                "use a different symbol or drop the existing series"
            )
        self._series[symbol] = _series_from_records(
            symbol, rows, bar_type
        )

    def register_csv(
        self,
        symbol: str,
        csv_path: Path,
        bar_type: BarType = BarType.BAR_1M,
        timestamp_unit: str = "ns",
        timestamp_column: str = "timestamp",
    ) -> None:
        """Load a CSV file. See :func:`_series_from_csv` for column
        and unit semantics."""
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise DataHandlerError(
                f"CSV file for {symbol!r} not found: {csv_path}"
            )
        if symbol in self._series:
            raise DataHandlerError(
                f"symbol {symbol!r} already registered"
            )
        self._series[symbol] = _series_from_csv(
            symbol,
            csv_path,
            bar_type=bar_type,
            timestamp_unit=timestamp_unit,
            timestamp_column=timestamp_column,
        )

    def register_directory(
        self,
        csv_dir: Path,
        bar_type: BarType = BarType.BAR_1M,
        timestamp_unit: str = "ns",
        filename_to_symbol: Optional[callable] = None,
    ) -> None:
        """Bulk-load every ``*.csv`` under ``csv_dir``.

        ``filename_to_symbol`` maps filename (without extension) to a
        symbol name. By default, the filename stem becomes the symbol.
        """
        if filename_to_symbol is None:
            def filename_to_symbol(name: str) -> str:
                return name
        csv_dir = Path(csv_dir)
        if not csv_dir.is_dir():
            raise DataHandlerError(f"not a directory: {csv_dir}")
        for path in sorted(csv_dir.glob("*.csv")):
            symbol = filename_to_symbol(path.stem)
            self.register_csv(
                symbol,
                path,
                bar_type=bar_type,
                timestamp_unit=timestamp_unit,
            )

    # ------------------------------------------------------------------
    # AbstractDataHandler API
    # ------------------------------------------------------------------

    def symbols(self) -> tuple[str, ...]:
        return tuple(self._series)

    def latest_bar(self, symbol: str) -> Optional[MarketEvent]:
        return self._latest_emitted.get(symbol)

    def get_bar(self, symbol: str, timestamp_ns: int) -> Optional[MarketEvent]:
        series = self._series.get(symbol)
        if series is None:
            raise UnknownSymbolError(f"unknown symbol {symbol!r}")
        if self._cursor_ns is not None and timestamp_ns > self._cursor_ns:
            raise PointInTimeLeakError(
                f"requested {timestamp_ns}ns but cursor is at "
                f"{self._cursor_ns}ns (lookahead)"
            )
        idx = np.searchsorted(series.timestamps_ns, timestamp_ns, side="right") - 1
        if idx < 0:
            return None
        return series.event_at(idx)

    def stream(self, queue: EventQueue) -> Iterator[MarketEvent]:
        """Yield MarketEvents in merged timestamp order.

        The cursor is advanced on every iteration, so
        ``point-in-time`` lookup is consistent across consumers of
        :meth:`latest_bar` and :meth:`get_bar`.
        """
        if not self._series:
            raise DataHandlerError("no sources registered")

        # The heap holds ``(timestamp_ns, idx, symbol)`` tuples; ties
        # are broken by symbol so deterministic ordering is preserved.
        heap: list[tuple[int, int, str]] = []
        for symbol, series in self._series.items():
            heapq.heappush(
                heap, (int(series.timestamps_ns[0]), 0, symbol)
            )

        while heap:
            ts_ns, idx, symbol = heapq.heappop(heap)
            series = self._series[symbol]
            try:
                event = series.event_at(idx)
            except EventValidationError as exc:
                raise DataHandlerError(
                    f"malformed row in source for {symbol!r}: {exc}"
                ) from exc
            self._cursor_ns = event.timestamp_ns
            self._latest_emitted[symbol] = event
            queue.put(event)
            yield event

            new_idx = series.advance()
            if new_idx < series.timestamps_ns.shape[0]:
                heapq.heappush(
                    heap,
                    (int(series.timestamps_ns[new_idx]), new_idx, symbol),
                )


__all__ = [
    "AbstractDataHandler",
    "HistoricCSVDataHandler",
]
