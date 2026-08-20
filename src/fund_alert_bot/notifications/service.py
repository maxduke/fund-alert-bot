"""Notification service fan-out."""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from typing import Any

from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.i18n import localize_actions, localize_text
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
        targets: list[tuple[str, str, NotificationChannel]] = []
        for channel in self._channels:
            channel_targets = getattr(channel, "target_keys", ())
            if not channel_targets:
                channel_targets = (channel.name,)
            for target_key in channel_targets:
                targets.append((str(target_key), channel.name, channel))
        self._targets = tuple(targets)

    @property
    def enabled_channel_names(self) -> tuple[str, ...]:
        """Return the enabled notification channel names."""
        return tuple(channel.name for channel in self._channels)

    @property
    def delivery_targets(self) -> tuple[tuple[str, str], ...]:
        """Return frozen-delivery keys and their channel names."""
        return tuple(
            (target_key, channel_name) for target_key, channel_name, _ in self._targets
        )

    async def send_target(
        self,
        target_key: str,
        *,
        title: str,
        body: str,
        telegram_actions: tuple[tuple[tuple[str, str], ...], ...] = (),
    ) -> NotificationResult:
        """Send one message to one durable notification target."""
        target = next(
            (item for item in self._targets if item[0] == target_key),
            None,
        )
        if target is None:
            return NotificationResult(
                channel="",
                success=False,
                detail="unknown_target",
            )

        message = NotificationMessage(
            title=localize_text(title),
            body=localize_text(body),
            telegram_actions=localize_actions(telegram_actions),
        )
        _, _, channel = target
        try:
            send_to = getattr(channel, "send_to", None)
            if callable(send_to):
                result = await send_to(target_key, message)
            else:
                result = await channel.send(message)
        except Exception as exc:  # noqa: BLE001
            result = NotificationResult(
                channel=channel.name,
                success=False,
                detail=f"unexpected_error={type(exc).__name__}",
            )
        if result.success:
            LOGGER.info("Notification sent through target=%s", target_key)
        else:
            LOGGER.warning(
                "Notification failed through target=%s detail=%s",
                target_key,
                result.detail or "unknown",
            )
        return result

    async def send_alert(
        self,
        *,
        title: str,
        body: str,
        telegram_actions: tuple[tuple[tuple[str, str], ...], ...] = (),
    ) -> list[NotificationResult]:
        """Send one alert to all enabled channels."""
        if not self._channels:
            LOGGER.warning("Notification skipped; no enabled notification channels")
            return []

        results: list[NotificationResult] = []
        for target_key, _, _ in self._targets:
            results.append(
                await self.send_target(
                    target_key,
                    title=title,
                    body=body,
                    telegram_actions=telegram_actions,
                )
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
