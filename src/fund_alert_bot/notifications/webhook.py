"""Generic webhook notification adapter."""

from __future__ import annotations

import asyncio

import requests

from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult

DEFAULT_HTTP_TIMEOUT_SECONDS = 10


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
        try:
            response = requests.post(
                self._url,
                json={
                    "title": message.title,
                    "body": message.body,
                },
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            return NotificationResult(
                channel=self.name,
                success=False,
                detail=f"request_error={type(exc).__name__}",
            )

        if response.status_code >= 400:
            return NotificationResult(
                channel=self.name,
                success=False,
                detail=f"http_status={response.status_code}",
            )

        return NotificationResult(channel=self.name, success=True, detail="sent")
