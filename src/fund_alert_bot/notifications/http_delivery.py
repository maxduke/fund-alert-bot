"""Shared HTTP delivery for the Bark, ntfy and webhook channels."""

from __future__ import annotations

from typing import Any

import requests

from fund_alert_bot.notifications.base import NotificationResult

DEFAULT_HTTP_TIMEOUT_SECONDS = 10


def post_notification(
    channel: str,
    url: str,
    *,
    timeout: float,
    **request_kwargs: Any,
) -> NotificationResult:
    """POST one notification and report the outcome.

    Details carry only the exception type or HTTP status, never the URL, because
    channel URLs embed secrets such as Bark device keys and webhook tokens.
    """

    try:
        response = requests.post(url, timeout=timeout, **request_kwargs)
    except requests.RequestException as exc:
        return NotificationResult(
            channel=channel,
            success=False,
            detail=f"request_error={type(exc).__name__}",
        )

    if response.status_code >= 400:
        return NotificationResult(
            channel=channel,
            success=False,
            detail=f"http_status={response.status_code}",
        )

    return NotificationResult(channel=channel, success=True, detail="sent")
