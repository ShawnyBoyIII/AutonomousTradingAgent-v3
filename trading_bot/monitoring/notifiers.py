"""Alert notifiers for external integrations (Slack, Discord, webhooks)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from trading_bot.config.settings import Settings


class AlertLevel(Enum):
    """Alert severity levels."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class AlertEvent:
    """Alert event data."""

    level: AlertLevel
    title: str
    message: str
    timestamp: str
    details: dict | None = None


class WebhookNotifier:
    """Generic webhook notifier base class."""

    def __init__(self, webhook_url: str | None = None) -> None:
        self.webhook_url = webhook_url
        self.enabled = webhook_url is not None and webhook_url.startswith("http")

    def send(self, event: AlertEvent) -> bool:
        """Send alert to webhook. Returns True if successful."""
        if not self.enabled:
            return False

        try:
            payload = self._format_payload(event)
            return self._post_webhook(payload)
        except Exception:
            return False

    def _format_payload(self, event: AlertEvent) -> dict:
        """Format alert event into webhook payload. Override in subclasses."""
        return {
            "level": event.level.value,
            "title": event.title,
            "message": event.message,
            "timestamp": event.timestamp,
            "details": event.details or {},
        }

    def _post_webhook(self, payload: dict) -> bool:
        """Post payload to webhook URL."""
        try:
            data = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "TradingBot/1.0",
            }
            request = Request(
                self.webhook_url,  # type: ignore[arg-type]
                data=data,
                headers=headers,
                method="POST",
            )

            with urlopen(request, timeout=10) as response:
                return response.status in (200, 201, 204)
        except (HTTPError, URLError, TimeoutError):
            return False


class SlackNotifier(WebhookNotifier):
    """Slack webhook notifier."""

    def _format_payload(self, event: AlertEvent) -> dict:
        """Format for Slack webhook."""
        color_map = {
            AlertLevel.INFO: "#36a64f",  # Green
            AlertLevel.WARNING: "#ff9900",  # Orange
            AlertLevel.CRITICAL: "#ff0000",  # Red
        }

        fields = [
            {
                "title": "Level",
                "value": event.level.value.upper(),
                "short": True,
            },
            {
                "title": "Time",
                "value": event.timestamp,
                "short": True,
            },
        ]

        if event.details:
            for key, value in event.details.items():
                fields.append({
                    "title": key.replace("_", " ").title(),
                    "value": str(value),
                    "short": True,
                })

        return {
            "attachments": [
                {
                    "color": color_map.get(event.level, "#808080"),
                    "title": f"🤖 {event.title}",
                    "text": event.message,
                    "fields": fields,
                    "footer": "Trading Bot",
                    "ts": event.timestamp,
                }
            ]
        }


class DiscordNotifier(WebhookNotifier):
    """Discord webhook notifier."""

    def _format_payload(self, event: AlertEvent) -> dict:
        """Format for Discord webhook."""
        color_map = {
            AlertLevel.INFO: 0x36a64f,  # Green
            AlertLevel.WARNING: 0xff9900,  # Orange
            AlertLevel.CRITICAL: 0xff0000,  # Red
        }

        embed = {
            "title": f"🤖 {event.title}",
            "description": event.message,
            "color": color_map.get(event.level, 0x808080),
            "timestamp": event.timestamp,
            "fields": [],
        }

        embed["fields"].append({
            "name": "Level",
            "value": event.level.value.upper(),
            "inline": True,
        })

        if event.details:
            for key, value in event.details.items():
                embed["fields"].append({
                    "name": key.replace("_", " ").title(),
                    "value": str(value)[:1000],  # Discord limit
                    "inline": True,
                })

        return {
            "embeds": [embed],
            "username": "Trading Bot",
        }


class AlertNotifier:
    """Multi-channel alert notifier."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.notifiers: list[WebhookNotifier] = []
        self._load_notifiers(settings)

    def _load_notifiers(self, settings: Settings | None) -> None:
        """Load configured notifiers from settings and environment.

        Priority: environment variable > settings > skip.
        """
        # Slack webhook (env > settings)
        slack_url = os.getenv("SLACK_WEBHOOK_URL")
        if not slack_url and settings is not None:
            slack_url = getattr(settings.alerts, "slack_webhook_url", "") or None
        if slack_url:
            self.notifiers.append(SlackNotifier(slack_url))

        # Discord webhook (env > settings)
        discord_url = os.getenv("DISCORD_WEBHOOK_URL")
        if not discord_url and settings is not None:
            discord_url = getattr(settings.alerts, "discord_webhook_url", "") or None
        if discord_url:
            self.notifiers.append(DiscordNotifier(discord_url))

        # Generic webhook (env > settings)
        generic_url = os.getenv("WEBHOOK_URL")
        if not generic_url and settings is not None:
            generic_url = getattr(settings.alerts, "webhook_url", "") or None
        if generic_url:
            self.notifiers.append(WebhookNotifier(generic_url))

    def notify(
        self,
        level: AlertLevel,
        title: str,
        message: str,
        details: dict | None = None,
    ) -> list[bool]:
        """Send notification to all configured channels.

        Returns list of success status for each notifier.
        """
        event = AlertEvent(
            level=level,
            title=title,
            message=message,
            timestamp=datetime.now(timezone.utc).isoformat(),
            details=details,
        )

        return [notifier.send(event) for notifier in self.notifiers]

    def info(self, title: str, message: str, details: dict | None = None) -> list[bool]:
        """Send info level alert."""
        return self.notify(AlertLevel.INFO, title, message, details)

    def warning(
        self, title: str, message: str, details: dict | None = None
    ) -> list[bool]:
        """Send warning level alert."""
        return self.notify(AlertLevel.WARNING, title, message, details)

    def critical(
        self, title: str, message: str, details: dict | None = None
    ) -> list[bool]:
        """Send critical level alert."""
        return self.notify(AlertLevel.CRITICAL, title, message, details)

    def has_notifiers(self) -> bool:
        """Check if any notifiers are configured."""
        return len(self.notifiers) > 0


def notify_alerts(
    alerts: list[dict],
    notifier: AlertNotifier | None = None,
) -> None:
    """Send webhook notifications for alerts.

    Args:
        alerts: List of alert dictionaries from check_alert_conditions()
        notifier: Optional custom notifier instance
    """
    if not notifier:
        notifier = AlertNotifier()

    if not notifier.has_notifiers():
        return

    level_map = {
        "warning": AlertLevel.WARNING,
        "critical": AlertLevel.CRITICAL,
    }

    for alert in alerts:
        level = level_map.get(alert.get("level", ""), AlertLevel.INFO)
        alert_type = alert.get("type", "unknown")
        message = alert.get("message", "")

        notifier.notify(
            level=level,
            title=f"Alert: {alert_type.replace('_', ' ').title()}",
            message=message,
            details={
                "value": alert.get("value"),
                "threshold": alert.get("threshold"),
            },
        )
