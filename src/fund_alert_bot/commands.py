"""Telegram command shell."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fund_alert_bot.checks import (
    DCA_RULE_TYPE,
    DRAW_DOWN_RULE_TYPE,
    PROFIT_RULE_TYPE,
    DcaCheckResult,
    DrawdownCheckResult,
    ProfitCheckResult,
    evaluate_dca_rules,
    evaluate_drawdown_rules,
    evaluate_profit_rules,
)
from fund_alert_bot.config import NotificationSettings
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
from fund_alert_bot.notifications.dispatch import send_alert_notifications
from fund_alert_bot.notifications.service import build_notification_service
from fund_alert_bot.rules.dca import normalize_weekday

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

LOGGER = logging.getLogger(__name__)

ADD_DRAWDOWN_USAGE = (
    "Usage: /add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>"
)
ADD_DCA_USAGE = "Usage: /add_dca <name> <weekday> <amount>"
ADD_PROFIT_USAGE = "Usage: /add_profit <asset_type> <symbol> <name> <cost> <thresholds>"
START_MESSAGE = "fund-alert-bot is running. Use /help to see available commands."
HELP_MESSAGE = "\n".join(
    (
        "Available commands:",
        "/start - Start the bot",
        "/help - Show available commands",
        "/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>",
        "/add_profit <asset_type> <symbol> <name> <cost> <thresholds>",
        "/add_dca <name> <weekday> <amount>",
        "/list - List configured rules",
        "/del <id> - Delete a configured rule",
        "/check - Run a manual check",
        "/test_notify - Send a test notification to all enabled channels",
    )
)
NO_RULES_CONFIGURED_MESSAGE = "No rules configured"
NO_DRAWDOWN_RULES_TO_CHECK_MESSAGE = "No enabled drawdown_from_high rules to check"
NO_RULES_TO_CHECK_MESSAGE = (
    "No enabled drawdown_from_high, profit_reminder, or dca_reminder rules to check"
)
TEST_NOTIFICATION_TITLE = "fund-alert-bot test"
TEST_NOTIFICATION_MESSAGE = "\n".join(
    (
        "🧪 Test notification",
        "",
        "• Source: fund-alert-bot",
        "• Purpose: channel connectivity check",
    )
)
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


@dataclass(frozen=True, slots=True)
class DcaCommand:
    """Parsed /add_dca command fields."""

    name: str
    weekday: str
    amount: int | float


@dataclass(frozen=True, slots=True)
class ProfitCommand:
    """Parsed /add_profit command fields."""

    asset_type: AssetType
    symbol: str
    name: str
    cost: float
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


def parse_add_profit_args(args: Sequence[str]) -> ProfitCommand:
    """Parse /add_profit arguments into a typed command object."""

    if len(args) != 5:
        raise CommandParseError(ADD_PROFIT_USAGE)

    raw_asset_type, symbol, name, raw_cost, raw_thresholds = args
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

    return ProfitCommand(
        asset_type=asset_type,
        symbol=symbol,
        name=name,
        cost=parse_profit_cost(raw_cost),
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


def parse_profit_cost(raw_cost: str) -> float:
    """Parse a positive profit reminder cost basis."""

    try:
        cost = float(raw_cost)
    except ValueError as exc:
        raise CommandParseError("cost must be a positive number") from exc

    if not math.isfinite(cost) or cost <= 0:
        raise CommandParseError("cost must be a positive number")

    return cost


def parse_add_dca_args(args: Sequence[str]) -> DcaCommand:
    """Parse /add_dca arguments into a typed command object."""

    if len(args) != 3:
        raise CommandParseError(ADD_DCA_USAGE)

    raw_name, raw_weekday, raw_amount = args
    name = raw_name.strip()
    if not name:
        raise CommandParseError("name must not be empty")

    try:
        weekday = normalize_weekday(raw_weekday)
    except ValueError as exc:
        raise CommandParseError(str(exc)) from exc

    return DcaCommand(
        name=name,
        weekday=weekday,
        amount=parse_dca_amount(raw_amount),
    )


def parse_dca_amount(raw_amount: str) -> int | float:
    """Parse a positive DCA amount."""

    try:
        amount = float(raw_amount)
    except ValueError as exc:
        raise CommandParseError("amount must be a positive number") from exc

    if not math.isfinite(amount) or amount <= 0:
        raise CommandParseError("amount must be a positive number")

    if amount.is_integer():
        return int(amount)
    return amount


def drawdown_params(command: DrawdownCommand) -> dict[str, object]:
    """Build the persisted params_json object for a drawdown rule."""

    return {
        "lookback_days": command.lookback_days,
        "thresholds": command.thresholds,
        "price_field": "close",
    }


def dca_params(command: DcaCommand) -> dict[str, object]:
    """Build the persisted params_json object for a DCA rule."""

    return {
        "weekday": command.weekday,
        "amount": command.amount,
    }


def profit_params(command: ProfitCommand) -> dict[str, object]:
    """Build the persisted params_json object for a profit reminder rule."""

    return {
        "cost": command.cost,
        "thresholds": command.thresholds,
    }


def format_rules_list(rows: Sequence[Any]) -> str:
    """Format rules for the /list command."""

    if not rows:
        return NO_RULES_CONFIGURED_MESSAGE

    lines = ["Configured rules:"]
    lines.extend(_format_rule_row(row) for row in rows)
    return "\n".join(lines)


def format_check_summary(
    result: DrawdownCheckResult,
    dca_result: DcaCheckResult | None = None,
    profit_result: ProfitCheckResult | None = None,
) -> str:
    """Format a clear manual check summary."""

    if dca_result is not None or profit_result is not None:
        return _format_combined_check_summary(result, dca_result, profit_result)

    if result.checked_rules == 0:
        return NO_DRAWDOWN_RULES_TO_CHECK_MESSAGE

    alert_count = len(result.notifications)
    parts = [
        "📋 Check summary",
        "",
        f"✅ Checked {result.checked_rules} drawdown_from_high rule(s).",
        f"🔔 New alerts: {alert_count}.",
    ]
    if alert_count == 0:
        parts.append("👌 No alerts triggered.")
    _append_drawdown_statuses(parts, result)
    if result.skipped_duplicates:
        parts.append(f"♻️ Duplicate alerts skipped: {result.skipped_duplicates}.")
    if result.no_data_skips:
        parts.append("")
        parts.append(f"⚠️ No-data skips: {len(result.no_data_skips)}.")
        for skip in result.no_data_skips:
            parts.append(f"• Rule {skip.rule_id} {skip.symbol}: {skip.message}")
    if result.errors:
        parts.append("")
        parts.append(f"❌ Errors: {len(result.errors)}.")
        for error in result.errors:
            parts.append(f"• Rule {error.rule_id} {error.symbol}: {error.message}")
    return "\n".join(parts)


def _format_combined_check_summary(
    drawdown_result: DrawdownCheckResult,
    dca_result: DcaCheckResult | None,
    profit_result: ProfitCheckResult | None,
) -> str:
    dca_checked = 0 if dca_result is None else dca_result.checked_rules
    profit_checked = 0 if profit_result is None else profit_result.checked_rules
    total_checked = drawdown_result.checked_rules + profit_checked + dca_checked
    if total_checked == 0:
        return NO_RULES_TO_CHECK_MESSAGE

    dca_notifications = [] if dca_result is None else dca_result.notifications
    profit_notifications = [] if profit_result is None else profit_result.notifications
    alert_count = (
        len(drawdown_result.notifications)
        + len(profit_notifications)
        + len(dca_notifications)
    )
    parts = [
        "📋 Check summary",
        "",
        f"✅ Checked {drawdown_result.checked_rules} drawdown_from_high rule(s).",
        f"✅ Checked {profit_checked} profit_reminder rule(s).",
        f"✅ Checked {dca_checked} dca_reminder rule(s).",
        f"🔔 New alerts: {alert_count}.",
    ]
    if alert_count == 0:
        parts.append("👌 No alerts triggered.")
    _append_drawdown_statuses(parts, drawdown_result)

    dca_duplicates = 0 if dca_result is None else dca_result.skipped_duplicates
    profit_duplicates = 0 if profit_result is None else profit_result.skipped_duplicates
    skipped_duplicates = (
        drawdown_result.skipped_duplicates + profit_duplicates + dca_duplicates
    )
    if skipped_duplicates:
        parts.append(f"♻️ Duplicate alerts skipped: {skipped_duplicates}.")

    profit_no_data_skips = [] if profit_result is None else profit_result.no_data_skips
    no_data_skips = [*drawdown_result.no_data_skips, *profit_no_data_skips]
    if no_data_skips:
        parts.append("")
        parts.append(f"⚠️ No-data skips: {len(no_data_skips)}.")
        for skip in no_data_skips:
            parts.append(f"• Rule {skip.rule_id} {skip.symbol}: {skip.message}")

    dca_errors = [] if dca_result is None else dca_result.errors
    profit_errors = [] if profit_result is None else profit_result.errors
    errors = [*drawdown_result.errors, *profit_errors, *dca_errors]
    if errors:
        parts.append("")
        parts.append(f"❌ Errors: {len(errors)}.")
        for error in errors:
            parts.append(f"• Rule {error.rule_id} {error.symbol}: {error.message}")

    return "\n".join(parts)


def _append_drawdown_statuses(
    parts: list[str],
    result: DrawdownCheckResult,
) -> None:
    if not result.statuses:
        return

    parts.append("")
    parts.append("📉 Current drawdowns")
    for status in result.statuses:
        name = f" · {status.name}" if status.name else ""
        parts.append(
            f"• Rule {status.rule_id} {status.symbol}{name}: "
            f"{status.drawdown:.1%} from high "
            f"{status.peak_price:.4g} on {status.peak_date}; "
            f"latest {status.latest_price:.4g} on {status.latest_date}."
        )


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
    if row["type"] == DCA_RULE_TYPE:
        return (
            f"id={row['id']} type={row['type']} name={row['name']} params={params_text}"
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


def get_update_chat_id(update: object) -> int | None:
    """Read the effective Telegram chat ID from an update-like object."""
    effective_chat = getattr(update, "effective_chat", None)
    chat_id = getattr(effective_chat, "id", None)
    return chat_id if isinstance(chat_id, int) else None


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
    notification_settings: NotificationSettings | None = None,
) -> list[CommandHandler[Any, ContextTypes.DEFAULT_TYPE]]:
    """Build Telegram command handlers with an allowlist guard."""
    from telegram.ext import CommandHandler

    allowed_user_ids = frozenset(allowed_user_ids)
    notification_settings = notification_settings or NotificationSettings()
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

    async def add_profit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_add_profit_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            rule_id = add_rule(
                connection,
                type=PROFIT_RULE_TYPE,
                symbol=command.symbol,
                name=command.name,
                asset_type=command.asset_type.value,
                params=profit_params(command),
            )

        await _reply_text(
            update,
            (
                f"Added profit rule id={rule_id} "
                f"asset_type={command.asset_type.value} "
                f"symbol={command.symbol} name={command.name} "
                f"cost={command.cost:.12g}"
            ),
        )

    async def add_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_add_dca_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            rule_id = add_rule(
                connection,
                type=DCA_RULE_TYPE,
                symbol=command.name,
                name=command.name,
                asset_type="dca",
                params=dca_params(command),
            )

        await _reply_text(
            update,
            (
                f"Added DCA rule id={rule_id} "
                f"name={command.name} weekday={command.weekday} "
                f"amount={command.amount}"
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
            result = evaluate_drawdown_rules(
                connection,
                market_data_provider,
                include_latest=True,
            )
            profit_result = evaluate_profit_rules(connection, market_data_provider)
            dca_result = evaluate_dca_rules(connection)

        notifications = [
            *result.notifications,
            *profit_result.notifications,
            *dca_result.notifications,
        ]
        if notifications:
            notification_service = build_notification_service(
                settings=notification_settings,
                telegram_bot=context.bot,
                telegram_chat_ids=_command_chat_ids(update),
            )
            dispatch_summary = await send_alert_notifications(
                sqlite_path=sqlite_path,
                notification_service=notification_service,
                notifications=notifications,
            )
        else:
            dispatch_summary = None

        response = format_check_summary(result, dca_result, profit_result)
        if dispatch_summary is not None and dispatch_summary.failed:
            response = (
                f"{response}\n"
                f"Notification delivery failures: {dispatch_summary.failed}."
            )

        await _reply_text(update, response)

    async def test_notify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        notification_service = build_notification_service(
            settings=notification_settings,
            telegram_bot=context.bot,
            telegram_chat_ids=_command_chat_ids(update),
        )
        results = await notification_service.send_alert(
            title=TEST_NOTIFICATION_TITLE,
            body=TEST_NOTIFICATION_MESSAGE,
        )
        channel_count = len(notification_service.enabled_channel_names)
        if channel_count == 0:
            await _reply_text(update, "No enabled notification channels.")
        elif any(not result.success for result in results):
            successful_channels = sum(1 for result in results if result.success)
            await _reply_text(
                update,
                (
                    f"Sent test notification to {successful_channels} of "
                    f"{channel_count} channel(s)."
                ),
            )
        else:
            await _reply_text(
                update,
                f"Sent test notification to {channel_count} channel(s).",
            )

    return [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("add_drawdown", add_drawdown),
        CommandHandler("add_profit", add_profit),
        CommandHandler("add_dca", add_dca),
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
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Register supported Telegram command handlers."""
    for handler in build_command_handlers(
        allowed_user_ids,
        sqlite_path=sqlite_path,
        market_data_provider=market_data_provider,
        notification_settings=notification_settings,
    ):
        application.add_handler(handler)


def create_application(
    *,
    token: str,
    allowed_user_ids: Collection[int],
    sqlite_path: str | Path = ":memory:",
    market_data_provider: MarketDataProvider | None = None,
    notification_settings: NotificationSettings | None = None,
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
        notification_settings=notification_settings,
    )
    return application


def _command_chat_ids(update: object) -> frozenset[int]:
    chat_id = get_update_chat_id(update)
    if chat_id is None:
        LOGGER.warning("Telegram command update has no effective chat")
        return frozenset()
    return frozenset({chat_id})
