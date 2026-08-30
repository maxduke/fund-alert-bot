"""Telegram notification adapter."""

from __future__ import annotations

import logging
from collections.abc import Collection
from hashlib import sha256
from typing import Any

from fund_alert_bot.notifications.base import (
    NotificationMessage,
    NotificationResult,
    split_telegram_text,
)

LOGGER = logging.getLogger(__name__)


class TelegramNotificationChannel:
    """Send notifications through an existing python-telegram-bot bot."""

    name = "telegram"

    def __init__(self, *, bot: Any, chat_ids: Collection[int]) -> None:
        self._bot = bot
        self._chat_ids = tuple(sorted(set(chat_ids)))

    @property
    def target_keys(self) -> tuple[str, ...]:
        """Return one durable target key per Telegram chat."""
        return tuple(f"telegram:{chat_id}" for chat_id in self._chat_ids)

    async def send(self, message: NotificationMessage) -> NotificationResult:
        results = [
            await self.send_to(target_key, message) for target_key in self.target_keys
        ]
        if not results:
            return NotificationResult(
                channel=self.name,
                success=False,
                detail="no_chat_ids",
            )
        return NotificationResult(
            channel=self.name,
            success=all(result.success for result in results),
            detail=(
                f"sent={sum(result.success for result in results)} "
                f"failed={sum(not result.success for result in results)}"
            ),
        )

    async def send_to(
        self,
        target_key: str,
        message: NotificationMessage,
        *,
        resume_sent_chunks: int | None = None,
        resume_body_fingerprint: str | None = None,
    ) -> NotificationResult:
        try:
            chat_id = int(target_key.removeprefix("telegram:"))
        except ValueError:
            return NotificationResult(
                channel=self.name,
                success=False,
                detail="invalid_chat_id",
            )
        if chat_id not in self._chat_ids:
            return NotificationResult(
                channel=self.name,
                success=False,
                detail="unknown_chat_id",
            )

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
        body_fingerprint = ""
        sent_chunks = 0
        try:
            body_fingerprint = sha256(message.body.encode("utf-8")).hexdigest()
            chunks = split_telegram_text(message.body)
            start_index = 0
            if (
                resume_sent_chunks is not None
                and resume_body_fingerprint == body_fingerprint
                and 0 <= resume_sent_chunks < len(chunks)
            ):
                start_index = resume_sent_chunks
            sent_chunks = start_index
            for index in range(start_index, len(chunks)):
                chunk = chunks[index]
                kwargs = {"chat_id": chat_id, "text": chunk}
                if reply_markup is not None and index == len(chunks) - 1:
                    kwargs["reply_markup"] = reply_markup
                await self._bot.send_message(**kwargs)
                sent_chunks += 1
            # ponytail: progress is checkpointed when the adapter returns;
            # write each chunk only if crash-window duplicates become material.
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "Telegram notification failed for chat_id=%s: %s",
                chat_id,
                type(exc).__name__,
            )
            return NotificationResult(
                channel=self.name,
                success=False,
                detail=f"unexpected_error={type(exc).__name__}",
                sent_chunks=sent_chunks,
                body_fingerprint=body_fingerprint,
            )
        return NotificationResult(channel=self.name, success=True, detail="sent")
