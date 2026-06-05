"""Notification dispatch helpers that persist delivery outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fund_alert_bot.checks import AlertNotification
from fund_alert_bot.db import (
    initialize_database,
    open_connection,
    record_alert_notification_result,
)
from fund_alert_bot.notifications.service import NotificationService


@dataclass(frozen=True, slots=True)
class NotificationDispatchSummary:
    """Summary of alert notification delivery attempts."""

    attempted: int
    delivered: int
    failed: int


async def send_alert_notifications(
    *,
    sqlite_path: str | Path,
    notification_service: NotificationService,
    notifications: list[AlertNotification],
) -> NotificationDispatchSummary:
    """Send alert notifications and record channel delivery results."""

    delivered = 0
    failed = 0
    for notification in notifications:
        results = await notification_service.send_alert(
            title=notification.title,
            body=notification.text,
        )
        if any(result.success for result in results):
            delivered += 1
        else:
            failed += 1

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            record_alert_notification_result(
                connection,
                event_id=notification.event_id,
                results=results,
            )

    return NotificationDispatchSummary(
        attempted=len(notifications),
        delivered=delivered,
        failed=failed,
    )
