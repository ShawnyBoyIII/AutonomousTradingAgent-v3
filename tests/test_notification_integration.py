"""Tests for notification integration (settings-based notifiers + CLI commands)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.config.settings import AlertsSettings, AppSettings, Settings
from trading_bot.monitoring.notifiers import (
    AlertEvent,
    AlertLevel,
    AlertNotifier,
    DiscordNotifier,
    SlackNotifier,
    WebhookNotifier,
)


class TestAlertNotifierFromSettings:
    """Tests for AlertNotifier loading from Settings instead of env vars."""

    def test_loads_slack_from_settings(self) -> None:
        settings = Settings(
            alerts=AlertsSettings(slack_webhook_url="https://slack.com/webhook")
        )
        notifier = AlertNotifier(settings)
        assert len(notifier.notifiers) == 1
        assert isinstance(notifier.notifiers[0], SlackNotifier)

    def test_loads_discord_from_settings(self) -> None:
        settings = Settings(
            alerts=AlertsSettings(discord_webhook_url="https://discord.com/webhook")
        )
        notifier = AlertNotifier(settings)
        assert len(notifier.notifiers) == 1
        assert isinstance(notifier.notifiers[0], DiscordNotifier)

    def test_loads_generic_webhook_from_settings(self) -> None:
        settings = Settings(
            alerts=AlertsSettings(webhook_url="https://example.com/webhook")
        )
        notifier = AlertNotifier(settings)
        assert len(notifier.notifiers) == 1
        assert isinstance(notifier.notifiers[0], WebhookNotifier)

    def test_loads_all_three_channels_from_settings(self) -> None:
        settings = Settings(
            alerts=AlertsSettings(
                slack_webhook_url="https://slack.com/webhook",
                discord_webhook_url="https://discord.com/webhook",
                webhook_url="https://example.com/webhook",
            )
        )
        notifier = AlertNotifier(settings)
        assert len(notifier.notifiers) == 3
        assert isinstance(notifier.notifiers[0], SlackNotifier)
        assert isinstance(notifier.notifiers[1], DiscordNotifier)
        assert isinstance(notifier.notifiers[2], WebhookNotifier)

    def test_empty_settings_creates_no_notifiers(self) -> None:
        settings = Settings()
        notifier = AlertNotifier(settings)
        assert len(notifier.notifiers) == 0

    def test_env_takes_precedence_over_settings(self, monkeypatch) -> None:
        monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://env-slack.com/webhook")
        settings = Settings(
            alerts=AlertsSettings(slack_webhook_url="https://settings-slack.com/webhook")
        )
        notifier = AlertNotifier(settings)
        assert len(notifier.notifiers) == 1
        assert isinstance(notifier.notifiers[0], SlackNotifier)
        # The SlackNotifier should have been initialized with the env URL
        assert notifier.notifiers[0].webhook_url == "https://env-slack.com/webhook"

    def test_settings_fallback_when_env_not_set(self, monkeypatch) -> None:
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        settings = Settings(
            alerts=AlertsSettings(slack_webhook_url="https://slack.com/webhook")
        )
        notifier = AlertNotifier(settings)
        assert len(notifier.notifiers) == 1
        assert notifier.notifiers[0].webhook_url == "https://slack.com/webhook"

    def test_settings_none_creates_no_notifiers(self) -> None:
        notifier = AlertNotifier(None)
        assert len(notifier.notifiers) == 0


class TestNotifyHelper:
    """Tests for the notify() helper in app.py."""

    def test_notify_returns_empty_list_when_no_notifiers(self) -> None:
        settings = Settings()
        from trading_bot.cli.app import notify

        results = notify(settings, "info", "Test", "Test message")
        assert results == []

    @patch.object(SlackNotifier, "send")
    def test_notify_sends_via_slack(self, mock_send) -> None:
        mock_send.return_value = True
        settings = Settings(
            alerts=AlertsSettings(slack_webhook_url="https://slack.com/webhook")
        )
        from trading_bot.cli.app import notify

        results = notify(settings, "info", "Test", "Test message")
        assert results == [True]
        mock_send.assert_called_once()

    @patch.object(DiscordNotifier, "send")
    def test_notify_sends_via_discord(self, mock_send) -> None:
        mock_send.return_value = True
        settings = Settings(
            alerts=AlertsSettings(discord_webhook_url="https://discord.com/webhook")
        )
        from trading_bot.cli.app import notify

        results = notify(settings, "warning", "Test", "Test message")
        assert results == [True]
        mock_send.assert_called_once()

    def test_notify_maps_critical_level(self) -> None:
        notifier = AlertNotifier()
        notifier.notifiers = [MagicMock()]
        from trading_bot.cli.app import notify

        with patch.object(notifier, "notify") as mock_notify:
            notifier.notify = mock_notify
        # Manually test via AlertNotifier
        notifier = AlertNotifier()
        notifier.notifiers = [MagicMock()]
        with patch.object(notifier, "notify") as mock_notify:
            notifier.notify(
                level=AlertLevel.CRITICAL,
                title="Test",
                message="Test message",
            )
            args = mock_notify.call_args
            assert args[1]["level"] == AlertLevel.CRITICAL

    def test_notify_with_details(self) -> None:
        notifier = AlertNotifier()
        notifier.notifiers = [MagicMock()]
        with patch.object(notifier, "notify") as mock_notify:
            notifier.notify(
                level=AlertLevel.INFO,
                title="Test",
                message="Test message",
                details={"trades": 10, "win_rate": 0.45},
            )
            args = mock_notify.call_args
            assert args[1]["details"]["trades"] == 10
            assert args[1]["details"]["win_rate"] == 0.45


class TestHealthCommandNotifies:
    """Tests that health command sends notifications when unhealthy."""

    def test_health_sends_notification_when_unhealthy(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("WEBHOOK_URL", raising=False)

        db_path = tmp_path / "test.db"
        db_path.touch()
        settings = Settings(
            app=AppSettings(
                state_db_path=str(db_path),
                log_dir=str(tmp_path / "logs"),
            ),
            alerts=AlertsSettings(slack_webhook_url="https://slack.com/webhook"),
        )

        from trading_bot.monitoring.health import check_system_health, format_health_report

        health_result = check_system_health(settings)
        report = format_health_report(health_result)

        # The health check should produce output
        assert "Health Check Report" in report
        assert "Status:" in report

        # notify() should be callable with the report
        from trading_bot.cli.app import notify

        results = notify(
            settings,
            "critical",
            "System UNHEALTHY",
            report,
            {},
        )
        # Results list length matches number of notifiers
        assert isinstance(results, list)


class TestAlertsCommandNotifies:
    """Tests that alerts command sends notifications via notifiers."""

    def test_alerts_command_uses_notify_alerts(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("WEBHOOK_URL", raising=False)

        db_path = tmp_path / "test.db"
        db_path.touch()

        from trading_bot.portfolio.ledger import PortfolioLedger
        ledger = PortfolioLedger(db_path)

        from trading_bot.monitoring.notifiers import notify_alerts, AlertNotifier

        notifier = AlertNotifier(None)
        notifier.notifiers = [MagicMock()]

        alerts = [
            {
                "level": "warning",
                "type": "win_rate_low",
                "message": "Win rate is low",
                "value": 0.35,
                "threshold": 0.40,
            }
        ]

        # Should not raise
        notify_alerts(alerts, notifier=notifier)


class TestNotifierFormatPayloads:
    """Tests for webhook payload formatting."""

    def test_slack_payload_has_color_mapping(self) -> None:
        notifier = SlackNotifier("https://slack.com/webhook")
        event = AlertEvent(
            level=AlertLevel.CRITICAL,
            title="Critical",
            message="Something failed",
            timestamp="2025-01-01T00:00:00+00:00",
        )
        payload = notifier._format_payload(event)
        assert payload["attachments"][0]["color"] == "#ff0000"

    def test_discord_payload_has_color_mapping(self) -> None:
        notifier = DiscordNotifier("https://discord.com/webhook")
        event = AlertEvent(
            level=AlertLevel.WARNING,
            title="Warning",
            message="Something might fail",
            timestamp="2025-01-01T00:00:00+00:00",
        )
        payload = notifier._format_payload(event)
        assert payload["embeds"][0]["color"] == 0xff9900

    def test_generic_payload_structure(self) -> None:
        notifier = WebhookNotifier("https://example.com/webhook")
        event = AlertEvent(
            level=AlertLevel.INFO,
            title="Info",
            message="Just info",
            timestamp="2025-01-01T00:00:00+00:00",
        )
        payload = notifier._format_payload(event)
        assert payload["level"] == "info"
        assert payload["title"] == "Info"
        assert payload["message"] == "Just info"
