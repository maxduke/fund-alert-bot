"""Telegram command shell."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fund_alert_bot.checks import (
    DRAW_DOWN_RULE_TYPE,
    DrawdownCheckResult,
    evaluate_drawdown_rules,
)
from fund_alert_bot.db import (
    add_rule,
    delete_rule,
    initialize_database,
    open_connection,
)
from fund_alert_bot.db import (
    list_rules as db_list_rules,
)
from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    AssetType,
    MarketDataProvider,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

LOGGER = logging.getLogger(__name__)

ADD_DRAWDOWN_USAGE = (
    "Usage: /add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>"
)
START_MESSAGE = "fund-alert-bot is running. Use /help to see available commands."
HELP_MESSAGE = "\n".join(
    (
        "Available commands:",
        "/start - Start the bot",
        "/help - Show available commands",
        "/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>",
        "/list - List configured rules",
        "/del <id> - Delete a configured rule",
        "/check - Run a manual check",
        "/test_notify - Send a test Telegram notification",
    )
)
NO_RULES_CONFIGURED_MESSAGE = "No rules configured"
NO_RULES_TO_CHECK_MESSAGE = "No enabled drawdown_from_high rules to check"
TEST_NOTIFICATION_MESSAGE = "Test Telegram notification from fund-alert-bot."
UNAUTHORIZED_MESSAGE = "You are not allowed to use this bot."


class CommandParseError(ValueError):
    """A user-facing Telegram command parsing error."""


@dataclass(frozen=True, slots=True)
class DrawdownCommand:
    """Parsed /add_drawdown command fields."""

    asset_type: AssetType
    symbol: str
    name: str
    lookback_days: int
    thresholds: list[float]


def parse_add_drawdown_args(args: Sequence[str]) -> DrawdownCommand:
    """Parse /add_drawdown arguments into a typed command object."""

    if len(args) != 5:
        raise CommandParseError(ADD_DRAWDOWN_USAGE)

    raw_asset_type, symbol, name, raw_lookback_days, raw_thresholds = args
    try:
        asset_type = AssetType(raw_asset_type)
    except ValueError as exc:
        valid_values = ", ".join(asset_type.value for asset_type in AssetType)
        raise CommandParseError(
            f"Invalid asset_type: {raw_asset_type}. Valid values: {valid_values}"
        ) from exc

    symbol = symbol.strip()
    name = name.strip()
    if not symbol:
        raise CommandParseError("symbol must not be empty")
    if not name:
        raise CommandParseError("name must not be empty")

    try:
        lookback_days = int(raw_lookback_days)
    except ValueError as exc:
        raise CommandParseError("lookback_days must be a positive integer") from exc
    if lookback_days <= 0:
        raise CommandParseError("lookback_days must be a positive integer")

    return DrawdownCommand(
        asset_type=asset_type,
        symbol=symbol,
        name=name,
        lookback_days=lookback_days,
        thresholds=parse_thresholds(raw_thresholds),
    )


def parse_thresholds(raw_thresholds: str) -> list[float]:
    """Parse comma-separated percent thresholds into decimal fractions."""

    pieces = [piece.strip() for piece in raw_thresholds.split(",")]
    if not pieces or any(not piece for piece in pieces):
        raise CommandParseError("thresholds must be comma-separated percentages")

    thresholds: list[float] = []
    for piece in pieces:
        try:
            threshold_percent = float(piece)
        except ValueError as exc:
            raise CommandParseError(
                "thresholds must be comma-separated percentages"
            ) from exc

        if threshold_percent <= 0 or threshold_percent >= 100:
            raise CommandParseError(
                "thresholds must be greater than 0 and less than 100"
            )
        thresholds.append(threshold_percent / 100)

    return thresholds


def drawdown_params(command: DrawdownCommand) -> dict[str, object]:
    """Build the persisted params_json object for a drawdown rule."""

    return {
        "lookback_days": command.lookback_days,
        "thresholds": command.thresholds,
        "price_field": "close",
    }


def format_rules_list(rows: Sequence[Any]) -> str:
    """Format rules for the /list command."""

    if not rows:
        return NO_RULES_CONFIGURED_MESSAGE

    lines = ["Configured rules:"]
    lines.extend(_format_rule_row(row) for row in rows)
    return "\n".join(lines)


def format_check_summary(result: DrawdownCheckResult) -> str:
    """Format a clear manual check summary."""

    if result.checked_rules == 0:
        return NO_RULES_TO_CHECK_MESSAGE

    alert_count = len(result.notifications)
    parts = [
        f"Checked {result.checked_rules} drawdown_from_high rule(s).",
        f"New alerts: {alert_count}.",
    ]
    if alert_count == 0:
        parts.append("No alerts triggered.")
    if result.skipped_duplicates:
        parts.append(f"Duplicate alerts skipped: {result.skipped_duplicates}.")
    if result.no_data_skips:
        parts.append(f"No-data skips: {len(result.no_data_skips)}.")
        for skip in result.no_data_skips:
            parts.append(f"Rule {skip.rule_id} {skip.symbol}: {skip.message}")
    if result.errors:
        parts.append(f"Errors: {len(result.errors)}.")
        for error in result.errors:
            parts.append(f"Rule {error.rule_id} {error.symbol}: {error.message}")
    return "\n".join(parts)


def get_start_message() -> str:
    """Return the current start message."""
    return START_MESSAGE


def _format_rule_row(row: Any) -> str:
    params = _load_params(row["params_json"])
    params_text = json.dumps(
        params,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        f"id={row['id']} "
        f"type={row['type']} "
        f"asset_type={row['asset_type']} "
        f"symbol={row['symbol']} "
        f"name={row['name']} "
        f"params={params_text}"
    )


def _load_params(params_json: str) -> dict[str, Any]:
    params = json.loads(params_json)
    if not isinstance(params, dict):
        raise ValueError("params_json must contain a JSON object")
    return params


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
        LOGGER.warning("TELEGRAM_ALLOWED_USER_IDS is empty; rejecting Telegram command")
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
    *,
    sqlite_path: str | Path = ":memory:",
    market_data_provider: MarketDataProvider | None = None,
) -> list[CommandHandler[Any, ContextTypes.DEFAULT_TYPE]]:
    """Build Telegram command handlers with an allowlist guard."""
    from telegram.ext import CommandHandler

    allowed_user_ids = frozenset(allowed_user_ids)
    if market_data_provider is None:
        market_data_provider = AkshareMarketDataProvider()

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

    async def add_drawdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_add_drawdown_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            rule_id = add_rule(
                connection,
                type=DRAW_DOWN_RULE_TYPE,
                symbol=command.symbol,
                name=command.name,
                asset_type=command.asset_type.value,
                params=drawdown_params(command),
            )

        await _reply_text(
            update,
            (
                f"Added drawdown rule id={rule_id} "
                f"asset_type={command.asset_type.value} "
                f"symbol={command.symbol} name={command.name}"
            ),
        )

    async def list_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if await reject_if_unauthorized(update, allowed_user_ids):
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            response = format_rules_list(db_list_rules(connection))

        await _reply_text(update, response)

    async def delete_rule_command(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        args = getattr(context, "args", ())
        if len(args) != 1:
            await _reply_text(update, "Usage: /del <id>")
            return
        try:
            rule_id = int(args[0])
        except ValueError:
            await _reply_text(update, "Rule id must be an integer")
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            deleted = delete_rule(connection, rule_id)

        if deleted:
            await _reply_text(update, f"Deleted rule id={rule_id}")
        else:
            await _reply_text(update, f"Rule id={rule_id} was not found")

    async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            result = evaluate_drawdown_rules(connection, market_data_provider)

        if update.effective_chat is None and result.notifications:
            LOGGER.warning("Telegram /check update has no effective chat")
        elif update.effective_chat is not None:
            for notification in result.notifications:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=notification.text,
                )

        await _reply_text(update, format_check_summary(result))

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
        CommandHandler("add_drawdown", add_drawdown),
        CommandHandler("list", list_rules),
        CommandHandler("del", delete_rule_command),
        CommandHandler("check", check),
        CommandHandler("test_notify", test_notify),
    ]


def register_command_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    allowed_user_ids: Collection[int],
    *,
    sqlite_path: str | Path = ":memory:",
    market_data_provider: MarketDataProvider | None = None,
) -> None:
    """Register supported Telegram command handlers."""
    for handler in build_command_handlers(
        allowed_user_ids,
        sqlite_path=sqlite_path,
        market_data_provider=market_data_provider,
    ):
        application.add_handler(handler)


def create_application(
    *,
    token: str,
    allowed_user_ids: Collection[int],
    sqlite_path: str | Path = ":memory:",
    market_data_provider: MarketDataProvider | None = None,
    post_init: Callable[
        [Application[Any, Any, Any, Any, Any, Any]],
        Awaitable[None],
    ]
    | None = None,
    post_shutdown: Callable[
        [Application[Any, Any, Any, Any, Any, Any]],
        Awaitable[None],
    ]
    | None = None,
) -> Application[Any, Any, Any, Any, Any, Any]:
    """Create a python-telegram-bot application for the command shell."""
    from telegram.ext import Application

    if not token:
        msg = "TELEGRAM_BOT_TOKEN is required"
        raise ValueError(msg)

    if not allowed_user_ids:
        LOGGER.warning("TELEGRAM_ALLOWED_USER_IDS is empty; all commands are disabled")

    application_builder = Application.builder().token(token)
    if post_init is not None:
        application_builder.post_init(post_init)
    if post_shutdown is not None:
        application_builder.post_shutdown(post_shutdown)

    application = application_builder.build()
    register_command_handlers(
        application,
        allowed_user_ids,
        sqlite_path=sqlite_path,
        market_data_provider=market_data_provider,
    )
    return application
