"""Tests for webhook alert notifiers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.monitoring.notifiers import (
    AlertEvent,
    AlertLevel,
    AlertNotifier,
    DiscordNotifier,
    SlackNotifier,
    WebhookNotifier,
    notify_alerts,
)


class TestWebhookNotifier:
    """Tests for generic webhook notifier."""

    def test_init_without_url_is_disabled(self) -> None:
        notifier = WebhookNotifier(None)
        assert not notifier.enabled

    def test_init_with_url_is_enabled(self) -> None:
        notifier = WebhookNotifier("https://example.com/webhook")
        assert notifier.enabled

    def test_send_returns_false_when_disabled(self) -> None:
        notifier = WebhookNotifier(None)
        event = AlertEvent(
            level=AlertLevel.INFO,
            title="Test",
            message="Test message",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        result = notifier.send(event)
        assert result is False

    @patch("trading_bot.monitoring.notifiers.urlopen")
    def test_send_success(self, mock_urlopen) -> None:
        mock_response = MagicMock()
        mock_response.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_response

        notifier = WebhookNotifier("https://example.com/webhook")
        event = AlertEvent(
            level=AlertLevel.INFO,
            title="Test",
            message="Test message",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        result = notifier.send(event)

        assert result is True
        mock_urlopen.assert_called_once()

    @patch("trading_bot.monitoring.notifiers.urlopen")
    def test_send_failure_on_http_error(self, mock_urlopen) -> None:
        from urllib.error import HTTPError

        mock_urlopen.side_effect = HTTPError(
            url="https://example.com/webhook",
            code=500,
            msg="Internal Error",
            hdrs={},
            fp=None,
        )

        notifier = WebhookNotifier("https://example.com/webhook")
        event = AlertEvent(
            level=AlertLevel.INFO,
            title="Test",
            message="Test message",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        result = notifier.send(event)

        assert result is False


class TestSlackNotifier:
    """Tests for Slack notifier."""

    def test_format_payload_structure(self) -> None:
        notifier = SlackNotifier("https://hooks.slack.com/webhook")
        event = AlertEvent(
            level=AlertLevel.WARNING,
            title="Test Alert",
            message="This is a test",
            timestamp="2025-01-01T00:00:00+00:00",
            details={"win_rate": "35%", "trades": 10},
        )

        payload = notifier._format_payload(event)

        assert "attachments" in payload
        assert len(payload["attachments"]) == 1
        attachment = payload["attachments"][0]
        assert "🤖 Test Alert" in attachment["title"]
        assert attachment["text"] == "This is a test"
        assert "color" in attachment
        assert "fields" in attachment

    def test_format_payload_critical_level(self) -> None:
        notifier = SlackNotifier("https://hooks.slack.com/webhook")
        event = AlertEvent(
            level=AlertLevel.CRITICAL,
            title="Critical Alert",
            message="Something went wrong",
            timestamp="2025-01-01T00:00:00+00:00",
        )

        payload = notifier._format_payload(event)
        attachment = payload["attachments"][0]

        assert attachment["color"] == "#ff0000"


class TestDiscordNotifier:
    """Tests for Discord notifier."""

    def test_format_payload_structure(self) -> None:
        notifier = DiscordNotifier("https://discord.com/api/webhooks/test")
        event = AlertEvent(
            level=AlertLevel.INFO,
            title="Test Alert",
            message="This is a test",
            timestamp="2025-01-01T00:00:00+00:00",
            details={"metric": "value"},
        )

        payload = notifier._format_payload(event)

        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert "🤖 Test Alert" in embed["title"]
        assert embed["description"] == "This is a test"
        assert "color" in embed
        assert "fields" in embed

    def test_format_payload_info_level(self) -> None:
        notifier = DiscordNotifier("https://discord.com/api/webhooks/test")
        event = AlertEvent(
            level=AlertLevel.INFO,
            title="Info Alert",
            message="Everything is fine",
            timestamp="2025-01-01T00:00:00+00:00",
        )

        payload = notifier._format_payload(event)
        embed = payload["embeds"][0]

        assert embed["color"] == 0x36A64F


class TestAlertNotifier:
    """Tests for multi-channel alert notifier."""

    def test_init_loads_notifiers_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.com/webhook")
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/webhook")

        notifier = AlertNotifier()

        assert len(notifier.notifiers) == 2
        assert any(isinstance(n, SlackNotifier) for n in notifier.notifiers)
        assert any(isinstance(n, DiscordNotifier) for n in notifier.notifiers)

    def test_has_notifiers_returns_true_when_configured(self, monkeypatch) -> None:
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.com/webhook")
        notifier = AlertNotifier()
        assert notifier.has_notifiers()

    def test_has_notifiers_returns_false_when_empty(self, monkeypatch) -> None:
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("WEBHOOK_URL", raising=False)

        notifier = AlertNotifier()
        assert not notifier.has_notifiers()

    @patch.object(SlackNotifier, "send")
    def test_notify_sends_to_all_notifiers(self, mock_send) -> None:
        mock_send.return_value = True

        notifier = AlertNotifier()
        notifier.notifiers = [SlackNotifier("https://slack.com/webhook")]

        results = notifier.notify(
            level=AlertLevel.WARNING,
            title="Test",
            message="Test message",
        )

        assert results == [True]
        mock_send.assert_called_once()

    def test_info_method_sends_info_level(self) -> None:
        notifier = AlertNotifier()
        notifier.notifiers = []

        with patch.object(notifier, "notify") as mock_notify:
            notifier.info("Title", "Message")
            mock_notify.assert_called_once()
            # First positional arg is level
            call_args = mock_notify.call_args[0]
            assert call_args[0] == AlertLevel.INFO

    def test_warning_method_sends_warning_level(self) -> None:
        notifier = AlertNotifier()
        notifier.notifiers = []

        with patch.object(notifier, "notify") as mock_notify:
            notifier.warning("Title", "Message")
            mock_notify.assert_called_once()
            call_args = mock_notify.call_args[0]
            assert call_args[0] == AlertLevel.WARNING

    def test_critical_method_sends_critical_level(self) -> None:
        notifier = AlertNotifier()
        notifier.notifiers = []

        with patch.object(notifier, "notify") as mock_notify:
            notifier.critical("Title", "Message")
            mock_notify.assert_called_once()
            call_args = mock_notify.call_args[0]
            assert call_args[0] == AlertLevel.CRITICAL


class TestNotifyAlerts:
    """Tests for notify_alerts function."""

    def test_notify_alerts_skips_when_no_notifiers(self) -> None:
        alerts = [
            {
                "level": "warning",
                "type": "win_rate_low",
                "message": "Win rate is low",
                "value": 0.35,
                "threshold": 0.40,
            }
        ]

        notifier = AlertNotifier()
        notifier.notifiers = []

        # Should not raise
        notify_alerts(alerts, notifier)

    @patch.object(AlertNotifier, "notify")
    def test_notify_alerts_maps_levels_correctly(self, mock_notify) -> None:
        alerts = [
            {
                "level": "critical",
                "type": "profit_factor_low",
                "message": "Profit factor is low",
                "value": 0.8,
                "threshold": 1.0,
            }
        ]

        notifier = AlertNotifier()
        notifier.notifiers = [MagicMock()]

        notify_alerts(alerts, notifier)

        mock_notify.assert_called_once()
        args = mock_notify.call_args
        assert args[1]["level"] == AlertLevel.CRITICAL

    @patch.object(AlertNotifier, "notify")
    def test_notify_alerts_includes_details(self, mock_notify) -> None:
        alerts = [
            {
                "level": "warning",
                "type": "win_rate_low",
                "message": "Win rate is 35%",
                "value": 0.35,
                "threshold": 0.40,
            }
        ]

        notifier = AlertNotifier()
        notifier.notifiers = [MagicMock()]

        notify_alerts(alerts, notifier)

        mock_notify.assert_called_once()
        args = mock_notify.call_args
        assert args[1]["details"]["value"] == 0.35
        assert args[1]["details"]["threshold"] == 0.40
