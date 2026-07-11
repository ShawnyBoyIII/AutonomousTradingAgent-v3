"""Tests for EOD fetch CLI marker format (C1 workstream).

Marker files must encode the interval set in the filename so that a backfill
for one interval (e.g. ``1d``) does not block a backfill for a different
interval (e.g. ``1m``) on the same date.

The CLI centralizes marker naming through ``_eod_marker_filename`` in
``trading_bot.cli.app``.
"""

from __future__ import annotations

from pathlib import Path


from trading_bot.cli.app import _eod_marker_filename


class TestMarkerFilename:
    def test_single_interval(self) -> None:
        path = _eod_marker_filename(Path("/tmp/store"), "2026-07-06", ["1d"])
        assert path == Path("/tmp/store/.last_eod_fetch_2026-07-06_1d.marker")

    def test_intervals_sorted(self) -> None:
        path = _eod_marker_filename(Path("/tmp/store"), "2026-07-06", ["1m", "1d"])
        assert path == Path("/tmp/store/.last_eod_fetch_2026-07-06_1d_1m.marker")

    def test_all_intervals(self) -> None:
        path = _eod_marker_filename(
            Path("/tmp/store"),
            "2026-07-06",
            ["1d", "1m", "quotes", "trades"],
        )
        assert path == Path(
            "/tmp/store/.last_eod_fetch_2026-07-06_1d_1m_quotes_trades.marker"
        )

    def test_empty_intervals_falls_back_to_date(self) -> None:
        path = _eod_marker_filename(Path("/tmp/store"), "2026-07-06", [])
        # CLI never passes empty in practice; the marker is just `<date>.marker`.
        assert path == Path("/tmp/store/.last_eod_fetch_2026-07-06.marker")

    def test_parent_is_store_root(self) -> None:
        root = Path("/some/store")
        path = _eod_marker_filename(root, "2026-07-06", ["1d"])
        assert path.parent == root


class TestMarkerFilenameCrossInterval:
    def test_different_intervals_produce_different_markers(self) -> None:
        """The bug: a 1d marker should NOT block a 1m run on the same date."""
        root = Path("/tmp/store")
        marker_1d = _eod_marker_filename(root, "2026-07-06", ["1d"])
        marker_1m = _eod_marker_filename(root, "2026-07-06", ["1m"])
        assert marker_1d != marker_1m
        assert marker_1d.name == ".last_eod_fetch_2026-07-06_1d.marker"
        assert marker_1m.name == ".last_eod_fetch_2026-07-06_1m.marker"
