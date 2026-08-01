"""News and earnings filter for avoiding high-risk events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclass
class NewsEvent:
    """A news event for a symbol."""

    symbol: str
    event_type: str  # "earnings", "fda", "merger", "upgrade", "downgrade", etc.
    timestamp: datetime
    sentiment: float = 0.0  # -1.0 to 1.0
    headline: str = ""
    source: str = ""


@dataclass
class RiskAssessment:
    """Risk assessment for trading a symbol."""

    symbol: str
    can_trade: bool
    risk_level: str  # "low", "medium", "high", "extreme"
    reasons: list[str] = field(default_factory=list)
    events: list[NewsEvent] = field(default_factory=list)
    hold_until: datetime | None = None  # Don't trade until after this time


class NewsFilter:
    """Filter symbols based on news and events.

    Prevents trading during high-risk events like:
    - Earnings releases (±1 day)
    - FDA decisions
    - M&A announcements
    - Major downgrades
    """

    # Earnings calendar (simplified - would come from API)
    EARNINGS_BUFFER_HOURS = 24  # Don't trade 24h before/after earnings

    def __init__(self) -> None:
        self.earnings_calendar: dict[str, datetime] = {}
        self.event_cache: dict[str, list[NewsEvent]] = {}

    def check_symbol(
        self,
        symbol: str,
        check_time: datetime | None = None,
    ) -> RiskAssessment:
        """Check if symbol is safe to trade.

        Returns RiskAssessment with trading recommendation.
        """
        check_time = check_time or datetime.now(timezone.utc)
        assessment = RiskAssessment(symbol=symbol, can_trade=True, risk_level="low")

        # Check 1: Upcoming earnings
        earnings_check = self._check_earnings(symbol, check_time)
        if not earnings_check["can_trade"]:
            assessment.can_trade = False
            assessment.risk_level = "high"
            assessment.reasons.append(earnings_check["reason"])
            assessment.hold_until = earnings_check["hold_until"]

        # Check 2: Recent extreme moves (indicates news)
        # This would check price history for gaps > 10%

        # Check 3: Cached events
        if symbol in self.event_cache:
            for event in self.event_cache[symbol]:
                hours_ago = (check_time - event.timestamp).total_seconds() / 3600

                if hours_ago < 24:  # Recent event
                    if event.event_type in ("earnings", "fda"):
                        assessment.can_trade = False
                        assessment.risk_level = "extreme"
                        assessment.reasons.append(f"Recent {event.event_type}: {event.headline[:50]}")
                    elif event.event_type in ("upgrade", "downgrade"):
                        assessment.risk_level = "medium"
                        assessment.reasons.append(f"Analyst action: {event.headline[:50]}")

        return assessment

    def _check_earnings(
        self,
        symbol: str,
        check_time: datetime,
    ) -> dict:
        """Check if symbol has earnings near check_time."""
        if symbol not in self.earnings_calendar:
            return {"can_trade": True, "reason": ""}

        earnings_time = self.earnings_calendar[symbol]
        hours_diff = abs((check_time - earnings_time).total_seconds() / 3600)

        if hours_diff < self.EARNINGS_BUFFER_HOURS:
            return {
                "can_trade": False,
                "reason": f"Earnings in {hours_diff:.1f} hours ({earnings_time.date()})",
                "hold_until": earnings_time + timedelta(hours=self.EARNINGS_BUFFER_HOURS),
            }

        return {"can_trade": True, "reason": ""}

    def add_earnings_date(self, symbol: str, date: datetime) -> None:
        """Add earnings date to calendar."""
        self.earnings_calendar[symbol] = date

    def add_news_event(self, event: NewsEvent) -> None:
        """Add a news event to cache."""
        if event.symbol not in self.event_cache:
            self.event_cache[event.symbol] = []
        self.event_cache[event.symbol].append(event)

        # Clean old events (>7 days)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        self.event_cache[event.symbol] = [
            e for e in self.event_cache[event.symbol]
            if e.timestamp > cutoff
        ]

    def filter_universe(
        self,
        symbols: list[str],
        check_time: datetime | None = None,
    ) -> tuple[list[str], list[RiskAssessment]]:
        """Filter a list of symbols, returning only safe ones.

        Returns:
            (safe_symbols, risk_assessments_for_all)
        """
        safe = []
        assessments = []

        for symbol in symbols:
            assessment = self.check_symbol(symbol, check_time)
            assessments.append(assessment)

            if assessment.can_trade:
                safe.append(symbol)

        return safe, assessments


def create_mock_earnings_calendar() -> dict[str, datetime]:
    """Create a mock earnings calendar for testing.

    In production, this would come from:
    - Alpha Vantage
    - Yahoo Finance
    - Earnings Whispers
    - Bloomberg API
    """
    now = datetime.now(timezone.utc)

    # Mock earnings dates
    calendar = {
        # Earnings today - should be blocked
        "AAPL": now - timedelta(hours=2),
        # Earnings tomorrow - should be blocked
        "TSLA": now + timedelta(hours=20),
        # Earnings next week - OK
        "MSFT": now + timedelta(days=5),
    }

    return calendar


def fetch_news_for_symbol(symbol: str) -> list[NewsEvent]:
    """Fetch recent news for a symbol.

    In production, integrate with:
    - NewsAPI
    - Bloomberg API
    - Benzinga
    - Twitter/X API
    - Reddit API
    """
    # Mock implementation
    return []


class EarningsCalendar:
    """Manages earnings calendar for risk filtering."""

    def __init__(self) -> None:
        self.dates: dict[str, datetime] = {}

    def is_earnings_today(self, symbol: str, tz=None) -> bool:
        """Check if symbol has earnings today."""
        if symbol not in self.dates:
            return False

        earnings_date = self.dates[symbol].date()
        today = datetime.now(tz).date() if tz else datetime.now().date()

        return earnings_date == today

    def is_earnings_this_week(self, symbol: str, tz=None) -> bool:
        """Check if symbol has earnings this week."""
        if symbol not in self.dates:
            return False

        earnings_date = self.dates[symbol].date()
        today = datetime.now(tz).date() if tz else datetime.now().date()

        # Get start of week
        start_of_week = today - timedelta(days=today.weekday())
        end_of_week = start_of_week + timedelta(days=6)

        return start_of_week <= earnings_date <= end_of_week

    def time_to_earnings(self, symbol: str) -> timedelta | None:
        """Get time until earnings (None if not scheduled or passed)."""
        if symbol not in self.dates:
            return None

        earnings_time = self.dates[symbol]
        now = datetime.now(timezone.utc)

        if earnings_time < now:
            return None

        return earnings_time - now

    def load_from_api(self, symbols: list[str]) -> None:
        """Load earnings dates from external API.

        Would integrate with:
        - Alpha Vantage Earnings Calendar
        - Yahoo Finance
        - Earnings Whispers
        """
        # Mock: Don't load anything
        pass
