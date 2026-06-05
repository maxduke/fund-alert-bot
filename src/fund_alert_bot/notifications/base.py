"""Shared notification types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """A notification ready to dispatch."""

    title: str
    body: str


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """Delivery result for a single notification channel."""

    channel: str
    success: bool
    detail: str = ""


class NotificationChannel(Protocol):
    """Protocol implemented by notification channel adapters."""

    name: str

    async def send(self, message: NotificationMessage) -> NotificationResult:
        """Send a notification message."""


def mask_config_value(value: str) -> str:
    """Return a log-safe representation of a sensitive config value."""
    if not value.strip():
        return "<unset>"
    return "<configured>"
