"""Telegram notification adapter."""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

from fund_alert_bot.notifications.base import NotificationMessage, NotificationResult

LOGGER = logging.getLogger(__name__)


class TelegramNotificationChannel:
    """Send notifications through an existing python-telegram-bot bot."""

    name = "telegram"

    def __init__(self, *, bot: Any, chat_ids: Collection[int]) -> None:
        self._bot = bot
        self._chat_ids = tuple(sorted(set(chat_ids)))

    async def send(self, message: NotificationMessage) -> NotificationResult:
        if not self._chat_ids:
            return NotificationResult(
                channel=self.name,
                success=False,
                detail="no_chat_ids",
            )

        sent = 0
        failed = 0
        for chat_id in self._chat_ids:
            try:
                await self._bot.send_message(chat_id=chat_id, text=message.body)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                LOGGER.warning(
                    "Telegram notification failed for chat_id=%s: %s",
                    chat_id,
                    type(exc).__name__,
                )
                continue
            sent += 1

        return NotificationResult(
            channel=self.name,
            success=failed == 0,
            detail=f"sent={sent} failed={failed}",
        )
