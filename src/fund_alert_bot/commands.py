"""Telegram command shell."""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

LOGGER = logging.getLogger(__name__)

START_MESSAGE = (
    "fund-alert-bot is running. Use /help to see available commands."
)
HELP_MESSAGE = "\n".join(
    (
        "Available commands:",
        "/start - Start the bot",
        "/help - Show available commands",
        "/list - List configured rules",
        "/check - Run a manual check",
        "/test_notify - Send a test Telegram notification",
    )
)
NO_RULES_CONFIGURED_MESSAGE = "No rules configured"
NO_RULES_TO_CHECK_MESSAGE = "No rules to check"
TEST_NOTIFICATION_MESSAGE = "Test Telegram notification from fund-alert-bot."
UNAUTHORIZED_MESSAGE = "You are not allowed to use this bot."


def get_start_message() -> str:
    """Return the current start message."""
    return START_MESSAGE


def is_allowed_telegram_user(
    user_id: int | None,
    allowed_user_ids: Collection[int],
) -> bool:
    """Return whether the Telegram user ID is explicitly allowed."""
    return user_id is not None and user_id in allowed_user_ids


def get_update_user_id(update: object) -> int | None:
    """Read the effective Telegram user ID from an update-like object."""
    effective_user = getattr(update, "effective_user", None)
    user_id = getattr(effective_user, "id", None)
    return user_id if isinstance(user_id, int) else None


def can_use_command(update: object, allowed_user_ids: Collection[int]) -> bool:
    """Return whether an update-like object may use bot commands."""
    return is_allowed_telegram_user(get_update_user_id(update), allowed_user_ids)


async def _reply_text(update: Update, text: str) -> None:
    if update.effective_message is None:
        LOGGER.warning("Telegram command update has no effective message")
        return

    await update.effective_message.reply_text(text)


async def reject_if_unauthorized(
    update: Update,
    allowed_user_ids: frozenset[int],
) -> bool:
    user_id = get_update_user_id(update)

    if not allowed_user_ids:
        LOGGER.warning(
            "TELEGRAM_ALLOWED_USER_IDS is empty; rejecting Telegram command"
        )
        await _reply_text(update, UNAUTHORIZED_MESSAGE)
        return True

    if not is_allowed_telegram_user(user_id, allowed_user_ids):
        LOGGER.warning(
            "Rejected Telegram command from unauthorized user_id=%s",
            user_id if user_id is not None else "unknown",
        )
        await _reply_text(update, UNAUTHORIZED_MESSAGE)
        return True

    return False


def build_command_handlers(
    allowed_user_ids: Collection[int],
) -> list[CommandHandler[Any, ContextTypes.DEFAULT_TYPE]]:
    """Build Telegram command handlers with an allowlist guard."""
    from telegram.ext import CommandHandler

    allowed_user_ids = frozenset(allowed_user_ids)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        await _reply_text(update, START_MESSAGE)

    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        await _reply_text(update, HELP_MESSAGE)

    async def list_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        await _reply_text(update, NO_RULES_CONFIGURED_MESSAGE)

    async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        await _reply_text(update, NO_RULES_TO_CHECK_MESSAGE)

    async def test_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        if update.effective_chat is None:
            LOGGER.warning("Telegram /test_notify update has no effective chat")
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=TEST_NOTIFICATION_MESSAGE,
        )

    return [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("list", list_rules),
        CommandHandler("check", check),
        CommandHandler("test_notify", test_notify),
    ]


def register_command_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    allowed_user_ids: Collection[int],
) -> None:
    """Register supported Telegram command handlers."""
    for handler in build_command_handlers(allowed_user_ids):
        application.add_handler(handler)


def create_application(
    *,
    token: str,
    allowed_user_ids: Collection[int],
) -> Application[Any, Any, Any, Any, Any, Any]:
    """Create a python-telegram-bot application for the command shell."""
    from telegram.ext import Application

    if not token:
        msg = "TELEGRAM_BOT_TOKEN is required"
        raise ValueError(msg)

    if not allowed_user_ids:
        LOGGER.warning("TELEGRAM_ALLOWED_USER_IDS is empty; all commands are disabled")

    application = Application.builder().token(token).build()
    register_command_handlers(application, allowed_user_ids)
    return application
