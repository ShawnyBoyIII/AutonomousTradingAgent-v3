"""Dynamic watchlist manager - auto-discovers and updates trading candidates.

This replaces static symbol lists with intelligent discovery based on:
- Sector rotation
- Market screening
- News filtering
- Technical setups
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    import pandas as pd

from trading_bot.strategy.market_screener import MarketScreener, ScreenResult
from trading_bot.config.settings import ScoutSettings
from trading_bot.strategy.news_filter import NewsFilter, RiskAssessment
from trading_bot.strategy.sector_rotation import (
    SectorRotationAnalysis,
    filter_symbols_by_sector_strength,
    get_best_sectors_for_trading,
)

logger = logging.getLogger(__name__)

# Symbol -> sector ETF symbol mapping (for sector filtering).
# Only covers MarketScreener.DEFAULT_UNIVERSE symbols; unknown symbols are
# kept (not penalized) when their sector can't be determined.
_SECTOR_MAP: dict[str, str] = {
    "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLC", "AMZN": "XLY", "META": "XLC",
    "TSLA": "XLY", "NVDA": "XLK", "NFLX": "XLC", "AMD": "XLK", "CRM": "XLK",
    "UBER": "XLY", "COIN": "XLF", "PLTR": "XLK", "SNOW": "XLK", "ROKU": "XLC",
    "SQ": "XLF", "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "GME": "XLY",
    "AMC": "XLY", "BB": "XLK", "NOK": "XLK", "HOOD": "XLF", "SPCE": "XLY",
    "LCID": "XLY", "RIVN": "XLY", "JNJ": "XLV", "PG": "XLP", "KO": "XLP",
    "PEP": "XLP", "WMT": "XLP", "HD": "XLY", "V": "XLF", "MA": "XLF",
    "XOM": "XLE", "CVX": "XLE", "OXY": "XLE", "COP": "XLE", "MPC": "XLE",
    "VLO": "XLE", "SLB": "XLE", "ET": "XLE", "JPM": "XLF", "BAC": "XLF",
    "WFC": "XLF", "GS": "XLF", "MS": "XLF", "C": "XLF", "BLK": "XLF",
    "AXP": "XLF", "PFE": "XLV", "MRK": "XLV", "LLY": "XLV", "UNH": "XLV",
    "ABBV": "XLV", "TMO": "XLV", "ABT": "XLV", "BABA": "XLY", "JD": "XLY",
    "PDD": "XLY", "NIO": "XLY", "XPEV": "XLY", "LI": "XLY", "BIDU": "XLC",
    "TME": "XLC",
}

_SECTOR_NAMES: dict[str, str] = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy",
    "XLI": "Industrials", "XLP": "Consumer Staples", "XLY": "Consumer Discretionary",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate",
    "XLV": "Health Care", "XLC": "Communication Services",
}


@dataclass
class WatchlistEntry:
    """A symbol in the dynamic watchlist."""

    symbol: str
    added_at: datetime
    reason: str
    score: float
    sector: str | None = None
    expected_hold_days: int = 1


@dataclass
class WatchlistUpdate:
    """Result of watchlist update cycle."""

    timestamp: datetime
    added: list[WatchlistEntry] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    current: list[WatchlistEntry] = field(default_factory=list)
    sectors_favored: list[str] = field(default_factory=list)


class DynamicWatchlist:
    """Automatically manages trading watchlist based on market conditions.

    Usage:
        1. Update sector analysis (daily)
        2. Screen market for setups (intraday)
        3. Filter by news/earnings
        4. Update watchlist file for burn-in script
    """

    def __init__(
        self,
        watchlist_path: str = "dynamic_watchlist.json",
        max_symbols: int = 20,
        min_score: float = 5.0,
        scout_settings: ScoutSettings | None = None,
    ) -> None:
        self.watchlist_path = Path(watchlist_path)
        self.scout_settings = scout_settings or ScoutSettings()
        self.max_symbols = min(max_symbols, self.scout_settings.max_universe_size)
        self.min_score = min_score

        self.screener = MarketScreener(min_price=self.scout_settings.min_price)
        self.news_filter = NewsFilter()

        self._entries: list[WatchlistEntry] = []
        self._last_update: datetime | None = None

    def update(
        self,
        data_provider: Callable[[str], "pd.DataFrame"],
        sector_analysis: SectorRotationAnalysis | None = None,
    ) -> WatchlistUpdate:
        """Perform full watchlist update cycle.

        Args:
            data_provider: Function to fetch data for a symbol
            sector_analysis: Optional sector rotation analysis

        Returns:
            WatchlistUpdate with changes
        """
        update = WatchlistUpdate(timestamp=datetime.now(timezone.utc))

        # Step 1: Screen market for setups
        logger.info("Screening market for setups...")
        screen_results = self._screen_market(data_provider)

        # Step 2: Filter by sector strength if available
        if sector_analysis:
            logger.info("Filtering by sector strength...")
            screen_results = self._filter_by_sector(screen_results, sector_analysis)
            update.sectors_favored = get_best_sectors_for_trading(sector_analysis)

        # Step 3: Filter by news/earnings
        logger.info("Checking news and earnings...")
        symbols = [r.symbol for r in screen_results if r.passed]
        safe_symbols, _ = self.news_filter.filter_universe(symbols)

        # Step 4: Create entries for qualified symbols
        new_entries = []
        for result in screen_results:
            if result.symbol in safe_symbols and result.score >= self.min_score:
                entry = WatchlistEntry(
                    symbol=result.symbol,
                    added_at=datetime.now(timezone.utc),
                    reason="; ".join(result.reasons[:2]),
                    score=result.score,
                    sector=result.metrics.get("sector"),
                )
                new_entries.append(entry)

        # Step 5: Merge with existing watchlist
        self._entries = self._merge_entries(self._entries, new_entries)

        # Step 6: Limit size
        if len(self._entries) > self.max_symbols:
            # Remove lowest scores
            self._entries.sort(key=lambda x: x.score, reverse=True)
            removed = self._entries[self.max_symbols:]
            self._entries = self._entries[:self.max_symbols]
            update.removed = [e.symbol for e in removed]

        # Step 7: Save
        self._save()

        update.current = self._entries
        update.added = [e for e in new_entries if e.symbol in [x.symbol for x in self._entries]]

        self._last_update = update.timestamp

        logger.info(f"Watchlist updated: {len(update.added)} added, {len(update.removed)} removed, {len(self._entries)} total")

        return update

    def quick_update_gappers(
        self,
        premarket_data: dict[str, "pd.DataFrame"],
    ) -> list[WatchlistEntry]:
        """Quick update for pre-market gap up symbols.

        Adds high-momentum gaps to watchlist immediately.
        """
        from trading_bot.strategy.market_screener import find_gap_up_symbols

        gaps = find_gap_up_symbols(premarket_data, min_gap_pct=3.0)

        added = []
        for gap in gaps[:5]:  # Top 5 gaps
            entry = WatchlistEntry(
                symbol=gap["symbol"],
                added_at=datetime.now(timezone.utc),
                reason=f"Pre-market gap +{gap['gap_pct']:.1f}%",
                score=70 + gap["gap_pct"],  # Higher gap = higher score
            )

            # Add if not already in list
            if not any(e.symbol == entry.symbol for e in self._entries):
                self._entries.append(entry)
                added.append(entry)

        if added:
            self._save()

        return added

    def get_symbols(self) -> list[str]:
        """Get current watchlist as list of symbols."""
        return [e.symbol for e in self._entries]

    def get_entries(self) -> list[WatchlistEntry]:
        """Get full watchlist entries with metadata."""
        return self._entries.copy()

    def remove_symbol(self, symbol: str, reason: str = "") -> bool:
        """Manually remove a symbol from watchlist."""
        original_len = len(self._entries)
        self._entries = [e for e in self._entries if e.symbol != symbol]

        if len(self._entries) < original_len:
            logger.info(f"Removed {symbol} from watchlist: {reason}")
            self._save()
            return True
        return False

    def export_for_burn_in(self, output_path: str | None = None) -> str:
        """Export watchlist in format for burn-in script.

        If the watchlist is empty, the existing file (if any) is preserved
        rather than being overwritten with an empty list — this prevents
        a failed discovery run from wiping a known-good universe.

        Returns path to exported file.
        """
        output_path = output_path or "state/universe.txt"
        path = Path(output_path)

        symbols = self.get_symbols()
        if not symbols:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            existing_count = len([s for s in existing.splitlines() if s.strip()])
            if existing_count:
                logger.warning(
                    f"Skipping export: watchlist empty, preserving existing "
                    f"{existing_count} symbols in {path}"
                )
                return str(path)
            # No existing file to preserve — fall through and write empty file
            # so callers can still detect "no symbols configured"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(symbols), encoding="utf-8")

        logger.info(f"Exported {len(symbols)} symbols to {path}")
        return str(path)

    def _screen_market(
        self,
        data_provider: Callable[[str], "pd.DataFrame"],
    ) -> list[ScreenResult]:
        """Screen market for trading candidates."""
        universe = self.screener.DEFAULT_UNIVERSE
        results = []

        for symbol in universe:
            try:
                frame = data_provider(symbol)
                if frame is None or len(frame) < 20:
                    continue

                result = self.screener.screen_symbol(symbol, frame)
                results.append(result)

            except Exception as e:
                logger.debug(f"Error screening {symbol}: {e}")
                continue

        return results

    def _filter_by_sector(
        self,
        results: list[ScreenResult],
        sector_analysis: SectorRotationAnalysis,
    ) -> list[ScreenResult]:
        """Filter screen results by sector strength.

        Boosts scores for symbols in top-ranked sectors and drops symbols
        whose sector is not in the top 5 (matching the sector_rotation
        strategy).
        """
        top_sectors = set(get_best_sectors_for_trading(sector_analysis, top_n=5))
        filtered: list[ScreenResult] = []

        for result in results:
            sector = _SECTOR_MAP.get(result.symbol)
            if sector is None:
                # Symbol not in our map — keep it (unknown sector, don't penalize)
                filtered.append(result)
                continue
            if sector not in top_sectors:
                # Sector is weak — drop this symbol
                continue
            # Sector is strong — boost score
            result.score += 10
            result.reasons.append(f"Strong sector: {_SECTOR_NAMES.get(sector, sector)}")
            filtered.append(result)

        return filtered

    def _merge_entries(
        self,
        existing: list[WatchlistEntry],
        new: list[WatchlistEntry],
    ) -> list[WatchlistEntry]:
        """Merge new entries with existing watchlist."""
        # Create dict for easy lookup
        entry_map = {e.symbol: e for e in existing}

        # Add or update with new entries
        for new_entry in new:
            if new_entry.symbol in entry_map:
                # Update score if higher
                if new_entry.score > entry_map[new_entry.symbol].score:
                    entry_map[new_entry.symbol] = new_entry
            else:
                entry_map[new_entry.symbol] = new_entry

        return list(entry_map.values())

    def _save(self) -> None:
        """Save watchlist to disk."""
        data = {
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "entries": [
                {
                    "symbol": e.symbol,
                    "added_at": e.added_at.isoformat(),
                    "reason": e.reason,
                    "score": e.score,
                    "sector": e.sector,
                }
                for e in self._entries
            ],
        }

        self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        self.watchlist_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load(self) -> bool:
        """Load watchlist from disk."""
        if not self.watchlist_path.exists():
            return False

        try:
            data = json.loads(self.watchlist_path.read_text(encoding="utf-8"))

            self._entries = [
                WatchlistEntry(
                    symbol=e["symbol"],
                    added_at=datetime.fromisoformat(e["added_at"]),
                    reason=e["reason"],
                    score=e["score"],
                    sector=e.get("sector"),
                )
                for e in data.get("entries", [])
            ]

            if data.get("last_update"):
                self._last_update = datetime.fromisoformat(data["last_update"])

            return True

        except Exception as e:
            logger.error(f"Error loading watchlist: {e}")
            return False


def create_watchlist_from_breakouts(
    symbols_data: dict[str, "pd.DataFrame"],
    min_score: float = 70.0,
) -> DynamicWatchlist:
    """Factory function to create watchlist from breakout setups.

    Args:
        symbols_data: Dict of symbol -> daily data
        min_score: Minimum score to include

    Returns:
        DynamicWatchlist with breakout candidates
    """
    from trading_bot.strategy.market_screener import screen_for_breakout_setups

    watchlist = DynamicWatchlist(min_score=min_score)

    breakouts = screen_for_breakout_setups(symbols_data)

    for result in breakouts:
        if result.score >= min_score:
            entry = WatchlistEntry(
                symbol=result.symbol,
                added_at=datetime.now(timezone.utc),
                reason="20-day breakout setup",
                score=result.score,
            )
            watchlist._entries.append(entry)

    watchlist._save()
    return watchlist


def update_burn_in_symbols(watchlist: DynamicWatchlist) -> None:
    """Update the burn-in symbol universe file."""
    symbols = watchlist.get_symbols()

    path = Path("state/universe.txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(symbols), encoding="utf-8")

    logger.info(f"Updated burn-in universe: {len(symbols)} symbols")
