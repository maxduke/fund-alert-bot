"""Bark notification adapter."""

from __future__ import annotations

import asyncio

from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.http_delivery import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    post_notification,
)


class BarkNotificationChannel:
    """Send notifications through Bark."""

    name = "bark"

    def __init__(
        self,
        *,
        server_url: str,
        device_key: str,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._device_key = device_key
        self._timeout = timeout

    async def send(self, message: NotificationMessage) -> NotificationResult:
        return await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: NotificationMessage) -> NotificationResult:
        return post_notification(
            self.name,
            f"{self._server_url}/push",
            json={
                "device_key": self._device_key,
                "title": message.title,
                "body": message.body,
            },
            timeout=self._timeout,
        )
