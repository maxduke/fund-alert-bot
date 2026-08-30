"""Shared notification types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

TELEGRAM_TEXT_LIMIT = 4096


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """A notification ready to dispatch."""

    title: str
    body: str
    telegram_actions: tuple[tuple[tuple[str, str], ...], ...] = ()


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


def split_telegram_text(text: str) -> tuple[str, ...]:
    """Split text into Telegram-sized chunks, preferring line boundaries."""
    chunks: list[str] = []
    while len(text) > TELEGRAM_TEXT_LIMIT:
        split_at = text.rfind("\n", 0, TELEGRAM_TEXT_LIMIT + 1)
        if split_at <= 0:
            split_at = TELEGRAM_TEXT_LIMIT
            chunks.append(text[:split_at])
            text = text[split_at:]
        else:
            chunks.append(text[:split_at])
            text = text[split_at + 1 :]
    if text or not chunks:
        chunks.append(text)
    return tuple(chunks)


def mask_config_value(value: str) -> str:
    """Return a log-safe representation of a sensitive config value."""
    if not value.strip():
        return "<unset>"
    return "<configured>"
