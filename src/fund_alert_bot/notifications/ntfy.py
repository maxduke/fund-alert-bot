"""ntfy notification adapter."""

from __future__ import annotations

import asyncio

import requests

from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult

DEFAULT_HTTP_TIMEOUT_SECONDS = 10


class NtfyNotificationChannel:
    """Send notifications through ntfy."""

    name = "ntfy"

    def __init__(
        self,
        *,
        server_url: str,
        topic: str,
        timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._topic = topic.strip("/")
        self._timeout = timeout

    async def send(self, message: NotificationMessage) -> NotificationResult:
        return await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: NotificationMessage) -> NotificationResult:
        try:
            response = requests.post(
                f"{self._server_url}/{self._topic}",
                data=message.body.encode("utf-8"),
                headers={"Title": message.title},
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
