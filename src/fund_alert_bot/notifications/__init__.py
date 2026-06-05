"""Notification adapter package."""

from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult
from fund_alert_bot.notifications.service import (
    NotificationService,
    build_notification_service,
)

__all__ = [
    "NotificationMessage",
    "NotificationResult",
    "NotificationService",
    "build_notification_service",
]
