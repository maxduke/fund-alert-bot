"""ntfy notification adapter."""

from __future__ import annotations

import asyncio
from email.header import Header

from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.http import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    post_notification,
)


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
        return post_notification(
            self.name,
            f"{self._server_url}/{self._topic}",
            data=message.body.encode("utf-8"),
            headers={"Title": _encode_header_value(message.title)},
            timeout=self._timeout,
        )


def _encode_header_value(value: str) -> str:
    """Encode non-ASCII ntfy header values using RFC 2047."""

    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode(maxlinelen=0)
    return value
