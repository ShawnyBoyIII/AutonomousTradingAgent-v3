"""Stage 2: data handlers — load, merge, point-in-time, stream ordering."""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from event_engine.events import BarType, MarketEvent
from event_engine.exceptions import (
    DataHandlerError,
    PointInTimeLeakError,
    UnknownSymbolError,
)
from event_engine.handlers import HistoricCSVDataHandler
from event_engine.queue import EventQueue


def _rows(base: int):
    return [
        (base + 20, 101.0, 102.0, 100.0, 101.5, 1_000.0, 0.01),
        (base + 30, 102.0, 103.0, 101.0, 102.5, 1_100.0, 0.01),
        (base + 40, 103.0, 104.0, 102.0, 103.5, 1_200.0, 0.01),
    ]


# ---------------------------------------------------------------------------
# In-memory ingest
# ---------------------------------------------------------------------------


def test_register_in_memory_series_adds_symbol(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    assert h.symbols() == ("AAPL",)
    assert h.latest_bar("AAPL") is None  # not streamed yet


def test_register_two_assets_yields_two_symbols(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    h.register_in_memory_series("MSFT", _rows(base_ts_ns + 1_000_000))
    assert set(h.symbols()) == {"AAPL", "MSFT"}


def test_register_in_memory_series_rejects_duplicate(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    with pytest.raises(DataHandlerError):
        h.register_in_memory_series("AAPL", _rows(base_ts_ns))


# ---------------------------------------------------------------------------
# Stream
# ---------------------------------------------------------------------------


def test_stream_emits_in_merged_chronological_order(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series(
        "AAPL",
        [(base_ts_ns + 30, 100, 110, 90, 100, 1000, 0.01)],
    )
    h.register_in_memory_series(
        "MSFT",
        [
            (base_ts_ns + 10, 200, 210, 190, 200, 1000, 0.02),
            (base_ts_ns + 40, 210, 220, 200, 210, 1000, 0.02),
        ],
    )
    queue = EventQueue()
    out = list(h.stream(queue))
    ts_seq = [e.timestamp_ns - base_ts_ns for e in out]
    assert ts_seq == [10, 30, 40]


def test_stream_pushes_each_event_into_queue(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    queue = EventQueue()
    out = list(h.stream(queue))
    # The handler ``put``s each event onto the queue *then* yields it.
    # A consumer that drains the queue (not the generator) sees the
    # same 3 events in the same order.
    drained = [queue.get() for _ in range(len(out))]
    assert drained == out
    assert len(out) == 3


def test_latest_bar_after_stream_reflects_last_emission(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    queue = EventQueue()
    list(h.stream(queue))
    last = h.latest_bar("AAPL")
    assert last is not None
    assert last.timestamp_ns == base_ts_ns + 40
    assert last.close == 103.5


def test_stream_with_no_sources_raises():
    h = HistoricCSVDataHandler()
    with pytest.raises(DataHandlerError):
        list(h.stream(EventQueue()))


# ---------------------------------------------------------------------------
# Point-in-time lookup
# ---------------------------------------------------------------------------


def test_get_bar_returns_latest_at_or_before_requested_ts(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    queue = EventQueue()
    list(h.stream(queue))  # advance cursor
    bar = h.get_bar("AAPL", base_ts_ns + 35)
    assert bar is not None
    assert bar.timestamp_ns == base_ts_ns + 30


def test_get_bar_returns_none_when_no_bar_at_or_before_ts(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    queue = EventQueue()
    list(h.stream(queue))
    assert h.get_bar("AAPL", base_ts_ns - 1) is None


def test_get_bar_before_any_stream_returns_first_bar(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    bar = h.get_bar("AAPL", base_ts_ns + 100)
    assert bar is not None
    # No stream yet, so the simulator cursor hasn't advanced; get_bar
    # should return the most-recent bar at or before the request —
    # the last row in ``_rows``.
    assert bar.timestamp_ns == base_ts_ns + 40


def test_get_bar_unknown_symbol_raises(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    queue = EventQueue()
    list(h.stream(queue))
    with pytest.raises(UnknownSymbolError):
        h.get_bar("X", base_ts_ns + 1)


def test_get_bar_in_future_raises_lookahead(base_ts_ns):
    h = HistoricCSVDataHandler()
    h.register_in_memory_series("AAPL", _rows(base_ts_ns))
    queue = EventQueue()
    list(h.stream(queue))
    with pytest.raises(PointInTimeLeakError):
        h.get_bar("AAPL", base_ts_ns + 100_000_000)


# ---------------------------------------------------------------------------
# Out-of-order input accepted (the queue enforces temporal order at consumption)
# ---------------------------------------------------------------------------


def test_unsorted_input_is_accepted_at_register(base_ts_ns):
    """``register_in_memory_series`` defensively sorts its input."""
    h = HistoricCSVDataHandler()
    rows = _rows(base_ts_ns)
    shuffled = [rows[2], rows[0], rows[1]]
    h.register_in_memory_series("AAPL", shuffled)
    q = EventQueue()
    out = list(h.stream(q))
    ts_seq = [e.timestamp_ns - base_ts_ns for e in out]
    assert ts_seq == [20, 30, 40]


# ---------------------------------------------------------------------------
# CSV ingest
# ---------------------------------------------------------------------------


def test_register_csv_round_trips(tmp_path: Path, base_ts_ns):
    csv_path = tmp_path / "AAPL.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["timestamp", "open", "high", "low", "close",
             "volume", "bid_ask_spread"]
        )
        for ts, o, h_, l, c, v, bas in _rows(base_ts_ns):
            writer.writerow([ts, o, h_, l, c, v, bas])

    h = HistoricCSVDataHandler()
    h.register_csv("AAPL", csv_path)
    q = EventQueue()
    out = list(h.stream(q))
    assert len(out) == 3
    assert out[0].timestamp_ns == base_ts_ns + 20


def test_register_csv_with_ms_units(tmp_path: Path):
    csv_path = tmp_path / "TSLA.csv"
    base_ms = 1_700_000_000_000  # ms
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["timestamp", "open", "high", "low", "close",
             "volume", "bid_ask_spread"]
        )
        for offset_ms, price in [(0, 100.0), (60_000, 101.0), (120_000, 102.0)]:
            writer.writerow([
                base_ms + offset_ms,
                price - 1, price + 1, price - 1, price,
                1_000, 0.01,
            ])

    h = HistoricCSVDataHandler()
    h.register_csv("TSLA", csv_path, timestamp_unit="ms")
    q = EventQueue()
    out = list(h.stream(q))
    assert len(out) == 3
    # 60-second spacing in ms should now be 60_000_000_000 in ns.
    gap = out[1].timestamp_ns - out[0].timestamp_ns
    assert gap == 60_000_000_000


def test_register_csv_missing_file_raises(tmp_path: Path):
    h = HistoricCSVDataHandler()
    with pytest.raises(DataHandlerError):
        h.register_csv("AAPL", tmp_path / "nope.csv")


def test_register_csv_handles_blank_cells_as_none(tmp_path: Path, base_ts_ns):
    csv_path = tmp_path / "AAPL.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["timestamp", "open", "high", "low", "close",
             "volume", "bid_ask_spread"]
        )
        # Mix of values and blanks — empties are parsed as ``None``
        # then ``MarketEvent`` rejects the row at construction.
        # The handler wraps the EventValidationError into a
        # DataHandlerError so callers only need to catch one.
        writer.writerow([base_ts_ns, 100, 101, 99, 100, "", 0.01])

    h = HistoricCSVDataHandler()
    h.register_csv("AAPL", csv_path)
    q = EventQueue()
    with pytest.raises(DataHandlerError):
        list(h.stream(q))


def test_register_directory_loads_all_csvs(tmp_path: Path, base_ts_ns):
    csv_dir = tmp_path / "feed"
    csv_dir.mkdir()
    for sym in ("AAPL", "MSFT"):
        with (csv_dir / f"{sym}.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(
                ["timestamp", "open", "high", "low", "close",
                 "volume", "bid_ask_spread"]
            )
            for offset in (10, 20):
                writer.writerow(
                    [base_ts_ns + offset,
                     100, 101, 99, 100, 1000, 0.01]
                )

    h = HistoricCSVDataHandler()
    h.register_directory(csv_dir)
    assert set(h.symbols()) == {"AAPL", "MSFT"}


def test_register_directory_uses_filename_to_symbol(tmp_path: Path, base_ts_ns):
    csv_dir = tmp_path / "feed"
    csv_dir.mkdir()
    (csv_dir / "AAPL_quotes.csv").write_text(
        "timestamp,open,high,low,close,volume,bid_ask_spread\n"
        f"{base_ts_ns + 10},100,101,99,100,1000,0.01\n"
    )
    h = HistoricCSVDataHandler()
    h.register_directory(
        csv_dir,
        filename_to_symbol=lambda name: name.split("_")[0],
    )
    assert h.symbols() == ("AAPL",)
