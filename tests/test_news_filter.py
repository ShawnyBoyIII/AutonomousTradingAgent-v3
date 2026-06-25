"""Tests for news and earnings filter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.strategy.news_filter import (
    NewsEvent,
    NewsFilter,
    RiskAssessment,
    calculate_news_sentiment,
    create_mock_earnings_calendar,
)


class TestNewsFilter:
    """Tests for news filter."""

    def test_filter_initialization(self) -> None:
        """Test filter initialization."""
        filter = NewsFilter()

        assert len(filter.earnings_calendar) == 0
        assert len(filter.event_cache) == 0

    def test_check_symbol_no_earnings(self) -> None:
        """Test checking symbol with no earnings."""
        filter = NewsFilter()

        assessment = filter.check_symbol("AAPL")

        assert isinstance(assessment, RiskAssessment)
        assert assessment.can_trade is True
        assert assessment.risk_level == "low"

    def test_check_symbol_with_upcoming_earnings(self) -> None:
        """Test checking symbol with upcoming earnings."""
        filter = NewsFilter()

        # Set earnings for tomorrow
        tomorrow = datetime.now(timezone.utc) + timedelta(hours=20)
        filter.add_earnings_date("TSLA", tomorrow)

        assessment = filter.check_symbol("TSLA")

        assert assessment.can_trade is False
        assert assessment.risk_level == "high"
        assert any("earnings" in r.lower() for r in assessment.reasons)

    def test_check_symbol_recent_earnings_event(self) -> None:
        """Test checking symbol with recent earnings in cache."""
        filter = NewsFilter()

        # Add recent earnings event
        event = NewsEvent(
            symbol="AAPL",
            event_type="earnings",
            timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
            sentiment=0.5,
            headline="AAPL beats earnings",
        )
        filter.add_news_event(event)

        assessment = filter.check_symbol("AAPL")

        assert assessment.can_trade is False
        assert assessment.risk_level == "extreme"

    def test_check_symbol_analyst_downgrade(self) -> None:
        """Test checking symbol with analyst downgrade."""
        filter = NewsFilter()

        event = NewsEvent(
            symbol="TSLA",
            event_type="downgrade",
            timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
            sentiment=-0.8,
            headline="TSLA downgraded to sell",
        )
        filter.add_news_event(event)

        assessment = filter.check_symbol("TSLA")

        # Should still be tradable but medium risk
        assert assessment.risk_level == "medium"

    def test_filter_universe(self) -> None:
        """Test filtering a universe of symbols."""
        filter = NewsFilter()

        # Block one symbol with earnings
        filter.add_earnings_date("TSLA", datetime.now(timezone.utc) + timedelta(hours=10))

        symbols = ["AAPL", "MSFT", "TSLA", "GOOGL"]
        safe, assessments = filter.filter_universe(symbols)

        assert "TSLA" not in safe
        assert "AAPL" in safe
        assert "MSFT" in safe
        assert len(assessments) == 4

    def test_add_earnings_date(self) -> None:
        """Test adding earnings date."""
        filter = NewsFilter()
        earnings_time = datetime(2025, 6, 20, 16, 0, 0, tzinfo=timezone.utc)

        filter.add_earnings_date("AAPL", earnings_time)

        assert "AAPL" in filter.earnings_calendar
        assert filter.earnings_calendar["AAPL"] == earnings_time

    def test_add_news_event_cleans_old(self) -> None:
        """Test that old events are cleaned from cache."""
        filter = NewsFilter()

        # Add old event
        old_event = NewsEvent(
            symbol="AAPL",
            event_type="earnings",
            timestamp=datetime.now(timezone.utc) - timedelta(days=10),
            sentiment=0.5,
        )
        filter.add_news_event(old_event)

        # Add new event
        new_event = NewsEvent(
            symbol="AAPL",
            event_type="upgrade",
            timestamp=datetime.now(timezone.utc),
            sentiment=0.8,
        )
        filter.add_news_event(new_event)

        # Should only have recent event
        assert len(filter.event_cache["AAPL"]) == 1


class TestSentimentCalculation:
    """Tests for sentiment calculation."""

    def test_positive_sentiment(self) -> None:
        """Test positive sentiment detection."""
        headlines = [
            "AAPL beats earnings expectations",
            "Bullish upgrade from Goldman Sachs",
            "Stock rallies on strong growth",
        ]

        sentiment = calculate_news_sentiment(headlines)

        assert sentiment > 0

    def test_negative_sentiment(self) -> None:
        """Test negative sentiment detection."""
        headlines = [
            "AAPL misses earnings target",
            "Bearish downgrade from Morgan Stanley",
            "Stock crashes on weak guidance",
        ]

        sentiment = calculate_news_sentiment(headlines)

        assert sentiment < 0

    def test_neutral_sentiment(self) -> None:
        """Test neutral sentiment with no keywords."""
        headlines = [
            "AAPL announces quarterly results",
            "Company releases new product",
        ]

        sentiment = calculate_news_sentiment(headlines)

        assert sentiment == 0.0

    def test_empty_headlines(self) -> None:
        """Test empty headlines."""
        sentiment = calculate_news_sentiment([])

        assert sentiment == 0.0


class TestMockEarningsCalendar:
    """Tests for mock earnings calendar."""

    def test_mock_calendar_creation(self) -> None:
        """Test creating mock earnings calendar."""
        calendar = create_mock_earnings_calendar()

        assert isinstance(calendar, dict)
        assert "AAPL" in calendar
        assert "TSLA" in calendar


class TestRiskAssessment:
    """Tests for RiskAssessment dataclass."""

    def test_risk_assessment_creation(self) -> None:
        """Test creating risk assessment."""
        assessment = RiskAssessment(
            symbol="AAPL",
            can_trade=False,
            risk_level="high",
            reasons=["Earnings today"],
            events=[],
        )

        assert assessment.symbol == "AAPL"
        assert assessment.can_trade is False
        assert assessment.risk_level == "high"
