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
        reply_markup = None
        if message.telegram_actions:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(label, callback_data=callback_data)
                        for label, callback_data in row
                    ]
                    for row in message.telegram_actions
                ]
            )
        for chat_id in self._chat_ids:
            try:
                kwargs = {"chat_id": chat_id, "text": message.body}
                if reply_markup is not None:
                    kwargs["reply_markup"] = reply_markup
                await self._bot.send_message(**kwargs)
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
