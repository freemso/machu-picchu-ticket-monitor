from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

from .config import Settings
from .models import AvailabilityChange
from .observability import NOTIFICATIONS_SENT

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    channel: str

    def enabled(self) -> bool:
        ...

    async def send(self, change: AvailabilityChange) -> None:
        ...


def format_message(change: AvailabilityChange) -> str:
    old = "unknown" if change.old_quantity is None else str(change.old_quantity)
    return (
        "Machu Picchu ticket availability increased\n"
        f"Date: {change.visit_date.isoformat()}\n"
        f"Route: {change.route_name} ({change.route})\n"
        f"Available: {change.new_quantity}\n"
        f"Previous: {old}\n"
        f"Seen at: {change.seen_at.isoformat()}"
    )


class TelegramNotifier:
    channel = "telegram"

    def __init__(self, settings: Settings):
        self.settings = settings

    def enabled(self) -> bool:
        return bool(self.settings.telegram_bot_token and self.settings.telegram_chat_id)

    async def send(self, change: AvailabilityChange) -> None:
        if not self.enabled():
            raise RuntimeError("Telegram notifier is not configured")
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                url,
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": format_message(change),
                    "disable_web_page_preview": True,
                },
            )
            response.raise_for_status()


class SlackNotifier:
    channel = "slack"

    def __init__(self, settings: Settings):
        self.settings = settings

    def enabled(self) -> bool:
        return bool(self.settings.slack_webhook_url)

    async def send(self, change: AvailabilityChange) -> None:
        if not self.enabled():
            raise RuntimeError("Slack notifier is not configured")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                self.settings.slack_webhook_url,
                json={"text": format_message(change)},
            )
            response.raise_for_status()


class EmailNotifier:
    channel = "email"

    def __init__(self, settings: Settings):
        self.settings = settings

    def enabled(self) -> bool:
        return bool(
            self.settings.smtp_host
            and self.settings.smtp_from
            and self.settings.smtp_to
        )

    async def send(self, change: AvailabilityChange) -> None:
        if not self.enabled():
            raise RuntimeError("Email notifier is not configured")
        await asyncio.to_thread(self._send_sync, change)

    def _send_sync(self, change: AvailabilityChange) -> None:
        message = EmailMessage()
        message["Subject"] = f"Machu Picchu availability: {change.route} on {change.visit_date}"
        message["From"] = self.settings.smtp_from
        message["To"] = self.settings.smtp_to
        message.set_content(format_message(change))

        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as smtp:
            if self.settings.smtp_use_tls:
                smtp.starttls()
            if self.settings.smtp_username and self.settings.smtp_password:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
            smtp.send_message(message)


class NotificationManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.notifiers: list[Notifier] = [
            TelegramNotifier(settings),
            EmailNotifier(settings),
            SlackNotifier(settings),
        ]

    def _configured_notifiers(self) -> list[Notifier]:
        return [notifier for notifier in self.notifiers if notifier.enabled()]

    def selected_notifiers(self, channels: set[str] | None = None) -> list[Notifier]:
        configured = self._configured_notifiers()
        if channels is not None:
            return [notifier for notifier in configured if notifier.channel in channels]

        preference = self.settings.preferred_notification.lower()
        if preference in {"all", "*"}:
            return configured
        return [notifier for notifier in configured if notifier.channel == preference]

    async def send(self, change: AvailabilityChange) -> list[str]:
        sent_channels: list[str] = []
        primary_notifiers = self.selected_notifiers()
        backup_notifiers = self.selected_notifiers(set(self.settings.backup_notification_values))

        if not primary_notifiers and not backup_notifiers:
            logger.warning(
                "no_notification_channels_configured",
                extra={
                    "preferred_notification": self.settings.preferred_notification,
                    "backup_notifications": self.settings.backup_notifications,
                },
            )
            return sent_channels

        for notifier in primary_notifiers:
            try:
                await notifier.send(change)
                NOTIFICATIONS_SENT.labels(channel=notifier.channel).inc()
                sent_channels.append(notifier.channel)
            except Exception as exc:
                logger.exception(
                    "notification_failed",
                    extra={"channel": notifier.channel, "error": str(exc)},
                )

        if sent_channels or self.settings.preferred_notification.lower() in {"all", "*"}:
            return sent_channels

        for notifier in backup_notifiers:
            try:
                await notifier.send(change)
                NOTIFICATIONS_SENT.labels(channel=notifier.channel).inc()
                sent_channels.append(notifier.channel)
                logger.info("backup_notification_sent", extra={"channel": notifier.channel})
            except Exception as exc:
                logger.exception(
                    "backup_notification_failed",
                    extra={"channel": notifier.channel, "error": str(exc)},
                )
        return sent_channels
