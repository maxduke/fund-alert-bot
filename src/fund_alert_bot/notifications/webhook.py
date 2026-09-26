"""Generic webhook notification adapter."""

from __future__ import annotations

import asyncio

from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    post_notification,
)


class WebhookNotificationChannel:
    """Send notifications to a generic webhook URL."""

    name = "webhook"

    def __init__(
        self,
        *,
        url: str,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        self._timeout = timeout

    async def send(self, message: NotificationMessage) -> NotificationResult:
        return await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: NotificationMessage) -> NotificationResult:
        return post_notification(
            self.name,
            self._url,
            json={
                "title": message.title,
                "body": message.body,
            },
            timeout=self._timeout,
        )
