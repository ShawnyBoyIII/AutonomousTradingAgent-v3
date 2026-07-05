"""Tests for DynamicWatchlist export behavior.

Network-free: exercises the non-destructive export logic that prevents
a failed discovery run (empty watchlist) from wiping the burn-in
symbols file.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from trading_bot.strategy.dynamic_watchlist import DynamicWatchlist, WatchlistEntry


def _make_watchlist(tmp_path: Path, entries=None) -> DynamicWatchlist:
    """Construct a DynamicWatchlist with optional pre-populated entries."""
    wl = DynamicWatchlist(
        watchlist_path=str(tmp_path / "watchlist.json"),
        max_symbols=20,
        min_score=0.0,
    )
    if entries is not None:
        wl._entries = list(entries)
    return wl


def _entry(symbol: str, score: float = 70.0) -> WatchlistEntry:
    return WatchlistEntry(
        symbol=symbol,
        added_at=datetime.now(timezone.utc),
        reason="test",
        score=score,
    )


class TestExportForBurnIn:
    def test_writes_symbols_when_watchlist_has_entries(self, tmp_path: Path) -> None:
        out = tmp_path / "burn-in-symbols.txt"
        wl = _make_watchlist(tmp_path, entries=[_entry("AAPL"), _entry("MSFT")])

        result = wl.export_for_burn_in(output_path=str(out))

        assert result == str(out)
        assert out.read_text(encoding="utf-8").splitlines() == ["AAPL", "MSFT"]

    def test_empty_watchlist_preserves_existing_file(self, tmp_path: Path) -> None:
        """Regression: a failed discover --export must not wipe the symbols file.

        Previously, export_for_burn_in unconditionally wrote the (empty)
        watchlist, replacing a known-good 150-symbol universe with 0 bytes.
        """
        out = tmp_path / "burn-in-symbols.txt"
        out.write_text("SPY\nQQQ\nAAPL\n", encoding="utf-8")

        wl = _make_watchlist(tmp_path, entries=[])  # empty watchlist, simulates 0 candidates
        result = wl.export_for_burn_in(output_path=str(out))

        # File must be preserved, not overwritten
        assert result == str(out)
        assert out.read_text(encoding="utf-8").splitlines() == ["SPY", "QQQ", "AAPL"]

    def test_empty_watchlist_preserves_large_existing_file(self, tmp_path: Path) -> None:
        """Specific regression for the 150-symbol universe case."""
        out = tmp_path / "burn-in-symbols.txt"
        symbols = [f"S{i:03d}" for i in range(150)]
        out.write_text("\n".join(symbols) + "\n", encoding="utf-8")

        wl = _make_watchlist(tmp_path, entries=[])
        wl.export_for_burn_in(output_path=str(out))

        assert out.read_text(encoding="utf-8").splitlines() == symbols

    def test_empty_watchlist_with_no_existing_file_writes_empty(self, tmp_path: Path) -> None:
        """If there's nothing to preserve, an empty file is acceptable.

        This is the "fresh-start" case: no symbols configured anywhere, so
        writing an empty file lets callers detect the missing configuration.
        """
        out = tmp_path / "burn-in-symbols.txt"
        wl = _make_watchlist(tmp_path, entries=[])

        wl.export_for_burn_in(output_path=str(out))

        assert out.exists()
        assert out.read_text(encoding="utf-8") == ""

    def test_non_empty_watchlist_overwrites_existing_file(self, tmp_path: Path) -> None:
        """A successful discovery must overwrite the previous file."""
        out = tmp_path / "burn-in-symbols.txt"
        out.write_text("OLD1\nOLD2\n", encoding="utf-8")

        wl = _make_watchlist(tmp_path, entries=[_entry("NEW1"), _entry("NEW2")])
        wl.export_for_burn_in(output_path=str(out))

        assert out.read_text(encoding="utf-8").splitlines() == ["NEW1", "NEW2"]

    def test_default_path_is_state_universe_txt(self, tmp_path: Path, monkeypatch) -> None:
        """Without an explicit output_path, export uses the runtime universe file."""
        # Run from tmp dir so the default-path file lands somewhere isolated
        monkeypatch.chdir(tmp_path)
        wl = _make_watchlist(tmp_path, entries=[_entry("AAPL")])

        wl.export_for_burn_in()  # no output_path

        default_file = tmp_path / "state" / "universe.txt"
        assert default_file.exists()
        assert default_file.read_text(encoding="utf-8").splitlines() == ["AAPL"]


class TestDefaultUniverse:
    """Smoke-checks that MarketScreener.DEFAULT_UNIVERSE is well-formed."""

    def test_universe_does_not_contain_known_renamed_ticker(self) -> None:
        """SQ (renamed to BLOCK then back to SQ but data-fetching issues) must be gone.

        This was the proximate cause of discover returning 0 candidates.
        """
        from trading_bot.strategy.market_screener import MarketScreener

        assert "SQ" not in MarketScreener.DEFAULT_UNIVERSE

    def test_universe_contains_high_liquidity_mega_caps(self) -> None:
        """Sanity: the refreshed list includes the most liquid traded names."""
        from trading_bot.strategy.market_screener import MarketScreener

        must_have = {"AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "NFLX"}
        missing = must_have - set(MarketScreener.DEFAULT_UNIVERSE)
        assert not missing, f"Missing mega-caps: {missing}"

    def test_universe_has_no_duplicates(self) -> None:
        from trading_bot.strategy.market_screener import MarketScreener

        universe = MarketScreener.DEFAULT_UNIVERSE
        assert len(universe) == len(set(universe)), "DEFAULT_UNIVERSE has duplicates"

    def test_universe_size_is_reasonable(self) -> None:
        from trading_bot.strategy.market_screener import MarketScreener

        # The refreshed list is ~150 verified tickers + 7 ETFs
        assert 100 <= len(MarketScreener.DEFAULT_UNIVERSE) <= 250
