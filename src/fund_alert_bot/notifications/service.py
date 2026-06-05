"""Notification service fan-out."""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from typing import Any

from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.notifications.bark import BarkNotificationChannel
from fund_alert_bot.notifications.base import (
    NotificationChannel,
    NotificationMessage,
    NotificationResult,
    mask_config_value,
)
from fund_alert_bot.notifications.ntfy import NtfyNotificationChannel
from fund_alert_bot.notifications.telegram import TelegramNotificationChannel
from fund_alert_bot.notifications.webhook import WebhookNotificationChannel

LOGGER = logging.getLogger(__name__)


class NotificationService:
    """Send each notification to all configured channels."""

    def __init__(self, channels: Sequence[NotificationChannel]) -> None:
        self._channels = tuple(channels)

    @property
    def enabled_channel_names(self) -> tuple[str, ...]:
        """Return the enabled notification channel names."""
        return tuple(channel.name for channel in self._channels)

    async def send_alert(self, *, title: str, body: str) -> list[NotificationResult]:
        """Send one alert to all enabled channels."""
        if not self._channels:
            LOGGER.warning("Notification skipped; no enabled notification channels")
            return []

        message = NotificationMessage(title=title, body=body)
        results: list[NotificationResult] = []
        for channel in self._channels:
            try:
                result = await channel.send(message)
            except Exception as exc:  # noqa: BLE001
                result = NotificationResult(
                    channel=channel.name,
                    success=False,
                    detail=f"unexpected_error={type(exc).__name__}",
                )

            results.append(result)
            if result.success:
                LOGGER.info("Notification sent through channel=%s", result.channel)
            else:
                LOGGER.warning(
                    "Notification failed through channel=%s detail=%s",
                    result.channel,
                    result.detail or "unknown",
                )

        return results


def build_notification_service(
    *,
    settings: NotificationSettings | None = None,
    telegram_bot: Any | None = None,
    telegram_chat_ids: Collection[int] = (),
) -> NotificationService:
    """Build a service for the enabled notification channels."""
    settings = settings or NotificationSettings()
    channels: list[NotificationChannel] = []

    telegram_chat_ids = frozenset(telegram_chat_ids)
    if telegram_bot is not None and telegram_chat_ids:
        channels.append(
            TelegramNotificationChannel(
                bot=telegram_bot,
                chat_ids=telegram_chat_ids,
            )
        )

    if settings.bark_enabled:
        if settings.bark_server_url and settings.bark_device_key:
            channels.append(
                BarkNotificationChannel(
                    server_url=settings.bark_server_url,
                    device_key=settings.bark_device_key,
                )
            )
        else:
            LOGGER.warning(
                "Bark notification channel enabled but missing config: "
                "server_url=%s device_key=%s",
                mask_config_value(settings.bark_server_url),
                mask_config_value(settings.bark_device_key),
            )

    if settings.ntfy_enabled:
        if settings.ntfy_server_url and settings.ntfy_topic:
            channels.append(
                NtfyNotificationChannel(
                    server_url=settings.ntfy_server_url,
                    topic=settings.ntfy_topic,
                )
            )
        else:
            LOGGER.warning(
                "ntfy notification channel enabled but missing config: "
                "server_url=%s topic=%s",
                mask_config_value(settings.ntfy_server_url),
                mask_config_value(settings.ntfy_topic),
            )

    if settings.webhook_enabled:
        if settings.webhook_url:
            channels.append(WebhookNotificationChannel(url=settings.webhook_url))
        else:
            LOGGER.warning(
                "Webhook notification channel enabled but missing config: url=%s",
                mask_config_value(settings.webhook_url),
            )

    LOGGER.info(
        "Enabled notification channels: %s",
        ", ".join(channel.name for channel in channels) or "none",
    )
    return NotificationService(channels)
