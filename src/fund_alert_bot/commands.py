"""Telegram command shell."""

from __future__ import annotations

import json
import logging
import math
import re
import secrets
import shlex
import sqlite3
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fund_alert_bot.checks import (
    DCA_RULE_TYPE,
    DRAW_DOWN_RULE_TYPE,
    PROFIT_RULE_TYPE,
    DcaCheckResult,
    DrawdownCheckResult,
    DrawdownPlanStatus,
    DrawdownPlanStatusResult,
    ProfitCheckResult,
    derive_plan_readiness,
    evaluate_dca_rules,
    evaluate_drawdown_rules,
    evaluate_profit_rules,
    latest_completed_open_date,
    read_drawdown_plan_statuses,
)
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import (
    add_drawdown_plan_rule,
    add_enhanced_dca_rule,
    add_position_profit_rule,
    add_rule,
    close_position_from_profit_event,
    delete_rule,
    find_enabled_drawdown_plan_conflict,
    get_active_drawdown_cycle,
    get_active_position_cycle,
    get_drawdown_plan_action_event,
    get_fund_settings,
    get_position_profit_event,
    get_position_snapshot,
    initialize_database,
    list_enabled_drawdown_plan_fund_symbols,
    list_enhanced_dca_statuses,
    list_manual_add_actions,
    list_pending_position_items,
    list_position_profit_statuses,
    list_position_snapshots,
    open_connection,
    reconcile_position_snapshot,
    record_manual_addition,
    skip_scheduled_dca_occurrence,
    upsert_fund_cutoff,
    upsert_fund_fee,
    upsert_position_snapshot,
)
from fund_alert_bot.db import (
    list_rules as db_list_rules,
)
from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    AssetType,
    CNMarketCalendar,
    FundNav,
    Instrument,
    MarketCalendar,
    MarketDataProvider,
    MarketDataProviderError,
    PriceBasis,
)
from fund_alert_bot.notifications.dispatch import send_alert_notifications
from fund_alert_bot.notifications.service import build_notification_service
from fund_alert_bot.rules.dca import normalize_weekday
from fund_alert_bot.rules.drawdown_plan import (
    DrawdownPlanConfig,
    DrawdownTier,
    evaluate_drawdown_plan,
    format_plan_amount,
    format_plan_percent,
    parse_drawdown_plan_config,
    required_history_start,
    validate_drawdown_plan_notification_size,
)
from fund_alert_bot.rules.profit import (
    build_position_profit_alert,
    format_profit_threshold_key,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

LOGGER = logging.getLogger(__name__)

ADD_DRAWDOWN_USAGE = (
    "Usage: /add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>"
)
ADD_DCA_USAGE = "\n".join(
    (
        "Usage: /add_dca <name> <weekday> <amount>",
        "   or: /add_dca <fund_symbol> <name> <weekday> <gross_amount> "
        "<rate:<percent>%|fixed:<RMB>> [holiday:next|holiday:skip]",
    )
)
ADD_PROFIT_USAGE = (
    "Usage: /add_profit <asset_type> <symbol> <name> <cost|auto> <thresholds>"
)
SET_FUND_FEE_USAGE = "Usage: /set_fund_fee <fund_symbol> <rate:<percent>%|fixed:<RMB>>"
SET_FUND_CUTOFF_USAGE = "Usage: /set_fund_cutoff <fund_symbol> <HH:MM>"
SYNC_POSITION_USAGE = "Usage: /sync_position <fund_symbol> <units> <average_unit_cost>"
ADD_DRAWDOWN_PLAN_USAGE = (
    "Usage: /add_drawdown_plan <reference_etf_symbol> <feeder_fund_symbol> "
    "<name> <tiers> [lookback:<calendar_days>]"
)
MARK_ADDED_USAGE = "Usage: /mark_added <plan_id> <tier_percentages>"
START_MESSAGE = "fund-alert-bot is running. Use /help to see available commands."
HELP_MESSAGE = "\n".join(
    (
        "Available commands:",
        "/start - Start the bot",
        "/help - Show available commands",
        "/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>",
        "/add_profit <asset_type> <symbol> <name> <cost|auto> <thresholds>",
        "/add_dca <name> <weekday> <amount> - Reminder only",
        "/add_dca <fund_symbol> <name> <weekday> <amount> <fee> "
        "[holiday:next|holiday:skip] - Fixed fund DCA estimate",
        "/dca_skip <rule_id> <due_date> - Deduction failed/not executed",
        "/set_fund_fee <fund_symbol> <rate:<percent>%|fixed:<RMB>>",
        "/set_fund_cutoff <fund_symbol> <HH:MM>",
        "/sync_position <fund_symbol> <units> <average_unit_cost>",
        "/add_drawdown_plan <reference_etf> <feeder_fund> <name> <tiers> "
        "[lookback:<days>]",
        "/mark_added <plan_id> <tier_percentages> - Record an addition you made",
        "/plans - Show investment-plan status",
        "/list - List configured rules",
        "/del <id> - Remove a configured rule",
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
    fund_symbol: str | None = None
    fee_mode: str | None = None
    fee_value: float | None = None
    holiday_policy: str | None = None


@dataclass(frozen=True, slots=True)
class ProfitCommand:
    """Parsed /add_profit command fields."""

    asset_type: AssetType
    symbol: str
    name: str
    cost: float | str
    thresholds: list[float]


@dataclass(frozen=True, slots=True)
class FundFeeCommand:
    """Parsed /set_fund_fee command fields."""

    fund_symbol: str
    fee_mode: str
    fee_value: float


@dataclass(frozen=True, slots=True)
class FundCutoffCommand:
    """Parsed /set_fund_cutoff command fields."""

    fund_symbol: str
    subscription_cutoff: str


@dataclass(frozen=True, slots=True)
class SyncPositionCommand:
    """Parsed /sync_position command fields."""

    fund_symbol: str
    units: float
    average_unit_cost: float


@dataclass(frozen=True, slots=True)
class DrawdownPlanCommand:
    """Parsed /add_drawdown_plan command fields."""

    reference_symbol: str
    investment_fund_symbol: str
    name: str
    params: dict[str, object]
    config: DrawdownPlanConfig


@dataclass(slots=True)
class DrawdownPlanDraft:
    """Short-lived Telegram pairing confirmation draft."""

    user_id: int
    chat_id: int
    expires_at: datetime
    command: DrawdownPlanCommand
    created_rule_id: int | None = None


@dataclass(frozen=True, slots=True)
class MarkAddedCommand:
    """Parsed explicit user statement that configured tiers were purchased."""

    plan_id: int
    tier_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManualAddSelection:
    """Server-loaded eligible tiers awaiting explicit confirmation."""

    plan_id: int
    event_id: int
    cycle_id: int
    fund_symbol: str
    name: str
    tiers: tuple[DrawdownTier, ...]
    readiness: str
    missing_setup: tuple[str, ...]
    cutoff: str
    market_date: date


@dataclass(slots=True)
class ManualAddDraft:
    """Short-lived user/chat-scoped manual-add confirmation."""

    user_id: int
    chat_id: int
    expires_at: datetime
    selection: ManualAddSelection
    completed_message: str | None = None


@dataclass(slots=True)
class PositionSyncDraft:
    """Short-lived reconciliation choice for a displayed pending set."""

    user_id: int
    chat_id: int
    expires_at: datetime
    command: SyncPositionCommand
    item_keys: tuple[str, ...]
    completed_message: str | None = None


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

    thresholds = parse_thresholds(raw_thresholds)
    if raw_cost.strip().lower() == "auto":
        if asset_type is not AssetType.CN_OPEN_FUND:
            raise CommandParseError("auto cost is only valid for cn_open_fund")
        symbol = _parse_fund_symbol(symbol)
        threshold_keys = [format_profit_threshold_key(value) for value in thresholds]
        if thresholds != sorted(thresholds) or len(threshold_keys) != len(
            set(threshold_keys)
        ):
            raise CommandParseError(
                "auto thresholds must be unique and strictly ascending"
            )
        cost: float | str = "auto"
    else:
        cost = parse_profit_cost(raw_cost)
    return ProfitCommand(
        asset_type=asset_type,
        symbol=symbol,
        name=name,
        cost=cost,
        thresholds=thresholds,
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

        if (
            not math.isfinite(threshold_percent)
            or threshold_percent <= 0
            or threshold_percent >= 100
        ):
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


def parse_set_fund_fee_args(args: Sequence[str]) -> FundFeeCommand:
    """Parse a shared feeder-fund subscription fee."""

    if len(args) != 2:
        raise CommandParseError(SET_FUND_FEE_USAGE)
    fund_symbol = _parse_fund_symbol(args[0])
    fee_mode, fee_value = _parse_fund_fee(args[1])
    return FundFeeCommand(fund_symbol, fee_mode, fee_value)


def _parse_fund_fee(raw_token: str) -> tuple[str, float]:
    raw_fee = raw_token.strip().lower()
    if raw_fee.startswith("rate:") and raw_fee.endswith("%"):
        fee_mode = "rate"
        raw_value = raw_fee[5:-1]
        divisor = 100
    elif raw_fee.startswith("fixed:"):
        fee_mode = "fixed"
        raw_value = raw_fee[6:]
        divisor = 1
    else:
        raise CommandParseError("fee must use rate:<percent>% or fixed:<RMB>")

    try:
        value = float(raw_value)
    except ValueError as exc:
        raise CommandParseError("fee must be a finite non-negative number") from exc
    if not math.isfinite(value) or value < 0:
        raise CommandParseError("fee must be a finite non-negative number")
    return fee_mode, value / divisor


def parse_set_fund_cutoff_args(args: Sequence[str]) -> FundCutoffCommand:
    """Parse a feeder-fund subscription cutoff in 24-hour time."""

    if len(args) != 2:
        raise CommandParseError(SET_FUND_CUTOFF_USAGE)
    fund_symbol = _parse_fund_symbol(args[0])
    cutoff = args[1].strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", cutoff):
        raise CommandParseError("cutoff must use 24-hour HH:MM format")
    return FundCutoffCommand(fund_symbol, cutoff)


def parse_sync_position_args(args: Sequence[str]) -> SyncPositionCommand:
    """Parse an exact sales-platform position snapshot."""

    if len(args) != 3:
        raise CommandParseError(SYNC_POSITION_USAGE)
    fund_symbol = _parse_fund_symbol(args[0])
    try:
        units = float(args[1])
        average_unit_cost = float(args[2])
    except ValueError as exc:
        raise CommandParseError(
            "units and average_unit_cost must be finite non-negative numbers"
        ) from exc
    if (
        not math.isfinite(units)
        or not math.isfinite(average_unit_cost)
        or units < 0
        or average_unit_cost < 0
    ):
        raise CommandParseError(
            "units and average_unit_cost must be finite non-negative numbers"
        )
    if (units == 0) != (average_unit_cost == 0):
        raise CommandParseError(
            "use positive units with positive cost, or exact 0 0 for a closed position"
        )
    return SyncPositionCommand(fund_symbol, units, average_unit_cost)


def parse_mark_added_args(args: Sequence[str]) -> MarkAddedCommand:
    """Parse selected drawdown tier percentages for a recorded manual add."""

    if len(args) != 2:
        raise CommandParseError(MARK_ADDED_USAGE)
    try:
        plan_id = int(args[0])
    except ValueError as exc:
        raise CommandParseError("plan_id must be a positive integer") from exc
    if plan_id <= 0:
        raise CommandParseError("plan_id must be a positive integer")
    raw_values = args[1].split(",")
    try:
        percentages = tuple(Decimal(value) for value in raw_values)
    except InvalidOperation as exc:
        raise CommandParseError(
            "tier percentages must be comma-separated numbers"
        ) from exc
    if not percentages or any(
        not value.is_finite() or value <= 0 or value >= 100 for value in percentages
    ):
        raise CommandParseError(
            "tier percentages must be unique numbers between 0 and 100"
        )
    tier_keys = tuple(format((value / 100).normalize(), "f") for value in percentages)
    if len(set(tier_keys)) != len(tier_keys):
        raise CommandParseError(
            "tier percentages must be unique numbers between 0 and 100"
        )
    return MarkAddedCommand(plan_id, tier_keys)


def load_manual_add_selection(
    connection: Any,
    *,
    plan_id: int,
    action_date: date,
    event_id: int | None = None,
    tier_keys: Sequence[str] = (),
    tier_index: int | None = None,
    select_all: bool = False,
) -> ManualAddSelection:
    """Load and validate eligible tiers from a stored same-day alert event."""

    active = get_active_drawdown_cycle(connection, plan_id)
    if active is None:
        raise CommandParseError(
            "No eligible same-day plan reminder was found. Use /sync_position "
            "after your platform position updates."
        )
    event = get_drawdown_plan_action_event(
        connection,
        rule_id=plan_id,
        event_id=event_id,
        data_date=action_date.isoformat(),
        cycle_id=int(active["id"]),
    )
    if event is None:
        raise CommandParseError(
            "No eligible same-day plan reminder was found. Use /sync_position "
            "after your platform position updates."
        )
    config = parse_drawdown_plan_config(
        reference_symbol=str(event["symbol"]),
        asset_type=str(event["asset_type"]),
        params=_load_params(str(event["params_json"])),
    )
    requested_keys = tuple(tier_keys)
    if len(set(requested_keys)) != len(requested_keys):
        raise CommandParseError("Selected tiers must be unique.")
    if event_id is None and requested_keys:
        configured_by_key = {tier.key: tier for tier in config.tiers}
        if any(key not in configured_by_key for key in requested_keys):
            raise CommandParseError("One or more selected tiers are not configured.")
        requested = tuple(configured_by_key[key] for key in requested_keys)
        event = get_drawdown_plan_action_event(
            connection,
            rule_id=plan_id,
            data_date=action_date.isoformat(),
            cycle_id=int(active["id"]),
            required_tier_keys=tuple(tier.key for tier in requested),
        )
        if event is None:
            raise CommandParseError(
                "Selected tiers are not all present in a same-day reminder."
            )
    payload = json.loads(str(event["payload_json"]))
    if (
        int(payload.get("cycle_id", -1)) != int(active["id"])
        or str(payload.get("data_date")) != action_date.isoformat()
    ):
        raise CommandParseError(
            "This reminder expired or its peak cycle changed. Use /sync_position."
        )
    eligible_keys = {
        str(item["key"])
        for item in payload.get("crossed_tiers", ())
        if isinstance(item, dict) and "key" in item
    }
    eligible = tuple(tier for tier in config.tiers if tier.key in eligible_keys)
    if select_all:
        selected = eligible
    elif tier_index is not None:
        selected = eligible[tier_index : tier_index + 1]
    else:
        eligible_by_key = {tier.key: tier for tier in eligible}
        if any(key not in eligible_by_key for key in requested_keys):
            raise CommandParseError(
                "Selected tiers are not all present in a same-day reminder."
            )
        selected = tuple(eligible_by_key[key] for key in requested_keys)
    if not selected:
        raise CommandParseError("No eligible tiers were selected.")
    already_added = {
        str(row["tier_key"])
        for row in list_manual_add_actions(connection, int(active["id"]))
    }
    if any(tier.key in already_added for tier in selected):
        raise CommandParseError("One or more selected tiers were already recorded.")
    readiness, missing_setup = derive_plan_readiness(
        connection,
        config.investment_fund_symbol,
    )
    settings = get_fund_settings(connection, config.investment_fund_symbol)
    cutoff = "15:00" if settings is None else str(settings["subscription_cutoff"])
    return ManualAddSelection(
        plan_id=plan_id,
        event_id=int(event["id"]),
        cycle_id=int(active["id"]),
        fund_symbol=config.investment_fund_symbol,
        name=str(event["name"]),
        tiers=selected,
        readiness=readiness,
        missing_setup=missing_setup,
        cutoff=cutoff,
        market_date=action_date,
    )


def format_manual_add_confirmation(selection: ManualAddSelection) -> str:
    """Show exactly what a later callback would record."""

    total = sum((tier.amount for tier in selection.tiers), start=0)
    lines = [
        "Confirm a completed manual addition",
        "",
        f"Plan: {selection.name} ({selection.plan_id})",
        f"Fund: {selection.fund_symbol}",
        "Selected tiers:",
        *(
            f"• -{format_plan_percent(tier.drawdown)} → "
            f"{format_plan_amount(tier.amount)}"
            for tier in selection.tiers
        ),
        f"Configured gross total: {format_plan_amount(total)}",
        f"Position estimate readiness: {selection.readiness}",
        "",
        "Continue only if you already submitted the fund subscription.",
        "The bot records your statement; it does not place or verify an order.",
    ]
    if selection.missing_setup:
        lines.append(f"Missing setup: {', '.join(selection.missing_setup)}")
    return "\n".join(lines)


def parse_add_drawdown_plan_args(args: Sequence[str]) -> DrawdownPlanCommand:
    """Parse a Reference ETF / feeder-fund drawdown plan."""

    try:
        words = shlex.split(" ".join(args))
    except ValueError as exc:
        raise CommandParseError(f"{ADD_DRAWDOWN_PLAN_USAGE}\n{exc}") from exc
    if len(words) not in {4, 5}:
        raise CommandParseError(ADD_DRAWDOWN_PLAN_USAGE)

    reference_symbol = _parse_fund_symbol(words[0])
    investment_fund_symbol = _parse_fund_symbol(words[1])
    name = words[2].strip()
    if not name:
        raise CommandParseError("name must not be empty")
    lookback_days = 365
    if len(words) == 5:
        option = words[4]
        if not option.startswith("lookback:"):
            raise CommandParseError("only trailing lookback:<calendar_days> is allowed")
        try:
            lookback_days = int(option.removeprefix("lookback:"))
        except ValueError as exc:
            raise CommandParseError("lookback must be a positive integer") from exc
        if lookback_days <= 0:
            raise CommandParseError("lookback must be a positive integer")

    tiers = _parse_drawdown_plan_tiers(words[3])
    params: dict[str, object] = {
        "investment_fund_symbol": investment_fund_symbol,
        "lookback_days": lookback_days,
        "tiers": tiers,
        "sma_window": 250,
        "sma_slope_window": 20,
    }
    try:
        config = parse_drawdown_plan_config(
            reference_symbol=reference_symbol,
            asset_type=AssetType.CN_ETF,
            params=params,
        )
        validate_drawdown_plan_notification_size(
            name=name,
            reference_symbol=reference_symbol,
            config=config,
        )
    except ValueError as exc:
        raise CommandParseError(str(exc)) from exc
    return DrawdownPlanCommand(
        reference_symbol,
        investment_fund_symbol,
        name,
        params,
        config,
    )


def _parse_drawdown_plan_tiers(raw_tiers: str) -> list[dict[str, int | float]]:
    pieces = raw_tiers.split(",")
    if not pieces or any(not piece for piece in pieces):
        raise CommandParseError("tiers must use percent:amount separated by commas")

    tiers: list[dict[str, int | float]] = []
    for piece in pieces:
        if piece.count(":") != 1:
            raise CommandParseError("tiers must use percent:amount separated by commas")
        raw_percent, raw_amount = piece.split(":")
        try:
            percent = float(raw_percent)
            amount = float(raw_amount)
        except ValueError as exc:
            raise CommandParseError("tier percent and amount must be numbers") from exc
        if not math.isfinite(percent) or percent <= 0 or percent >= 100:
            raise CommandParseError(
                "tier percent must be greater than 0 and less than 100"
            )
        if not math.isfinite(amount) or amount <= 0:
            raise CommandParseError("tier amount must be a positive finite number")
        tiers.append(
            {
                "drawdown": percent / 100,
                "amount": int(amount) if amount.is_integer() else amount,
            }
        )
    return tiers


def _parse_fund_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip()
    if not re.fullmatch(r"[0-9]{6}", symbol):
        raise CommandParseError("fund_symbol must be exactly 6 digits")
    return symbol


def parse_add_dca_args(args: Sequence[str]) -> DcaCommand:
    """Parse /add_dca arguments into a typed command object."""

    if len(args) == 3:
        raw_name, raw_weekday, raw_amount = args
        fund_symbol = None
        fee_mode = None
        fee_value = None
        holiday_policy = None
    else:
        try:
            words = shlex.split(" ".join(args))
        except ValueError as exc:
            raise CommandParseError(f"{ADD_DCA_USAGE}\n{exc}") from exc
        if len(words) not in {5, 6}:
            raise CommandParseError(ADD_DCA_USAGE)
        fund_symbol = _parse_fund_symbol(words[0])
        raw_name, raw_weekday, raw_amount = words[1:4]
        fee_mode, fee_value = _parse_fund_fee(words[4])
        holiday_policy = "next"
        if len(words) == 6:
            option = words[5].strip().lower()
            if option not in {"holiday:next", "holiday:skip"}:
                raise CommandParseError(
                    "holiday policy must be holiday:next or holiday:skip"
                )
            holiday_policy = option.removeprefix("holiday:")
    name = raw_name.strip()
    if not name:
        raise CommandParseError("name must not be empty")

    try:
        weekday = normalize_weekday(raw_weekday)
    except ValueError as exc:
        raise CommandParseError(str(exc)) from exc

    amount = parse_dca_amount(raw_amount)
    if fee_mode == "fixed" and fee_value is not None and fee_value >= amount:
        raise CommandParseError("fixed fee must be lower than the DCA amount")
    return DcaCommand(
        name=name,
        weekday=weekday,
        amount=amount,
        fund_symbol=fund_symbol,
        fee_mode=fee_mode,
        fee_value=fee_value,
        holiday_policy=holiday_policy,
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

    params: dict[str, object] = {
        "weekday": command.weekday,
        "amount": command.amount,
    }
    if command.holiday_policy is not None:
        params["holiday_policy"] = command.holiday_policy
    return params


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


def format_position_snapshot(row: Any, nav: FundNav | None) -> str:
    """Format an exact platform position and its latest dated fund value."""

    units = float(row["units"])
    lines = [
        f"Position synced for fund {row['fund_symbol']}.",
        f"Units: {units:.12g}",
        f"Average unit cost: {float(row['average_unit_cost']):.12g}",
        "Accuracy: exact (sales-platform sync)",
        f"Last sync: {row['last_synced_at']}",
        f"Applied estimates since sync: {int(row['estimates_since_sync'])}",
    ]
    if units == 0:
        lines.extend(("Position value: ¥0.00 (closed)", "Unit NAV was not requested."))
    elif nav is None:
        lines.append("Position value: unavailable (unit NAV could not be fetched)")
    else:
        lines.extend(
            (
                f"Latest unit NAV: {nav.value:.12g} on {nav.date} ({nav.source})",
                f"Position value: ¥{units * nav.value:,.2f}",
            )
        )
    lines.append(
        "Reminder: sync again after any redemption, distribution, unrecorded "
        "purchase, fee mismatch, or visible platform difference."
    )
    return "\n".join(lines)


def format_fund_fee(row: Any) -> str:
    """Format one stored feeder-fund fee setting."""

    value = float(row["fee_value"])
    fee = (
        f"rate:{value * 100:.12g}%"
        if row["fee_mode"] == "rate"
        else f"fixed:{value:.12g} RMB"
    )
    return (
        f"Updated fund {row['fund_symbol']} fee to {fee}. "
        f"Subscription cutoff: {row['subscription_cutoff']}. "
        "The change applies only to future estimates."
    )


def build_drawdown_plan_preview(
    connection: Any,
    market_data_provider: MarketDataProvider,
    command: DrawdownPlanCommand,
    *,
    today: date,
) -> str:
    """Build a read-only pairing preview without storing plan state."""

    readiness, missing_setup = derive_plan_readiness(
        connection,
        command.investment_fund_symbol,
    )
    lines = [
        "📋 Confirm Drawdown Add Plan",
        "",
        f"Reference ETF: {command.reference_symbol}",
        f"Investment feeder fund: {command.investment_fund_symbol}",
        f"Display name: {command.name}",
        f"Lookback: {command.config.lookback_days} calendar days",
        "Tiers (incremental):",
        *(
            f"-{format_plan_percent(tier.drawdown)} → {format_plan_amount(tier.amount)}"
            for tier in command.config.tiers
        ),
        "Maximum one-cycle total: "
        f"{format_plan_amount(sum(tier.amount for tier in command.config.tiers))}",
        "MA250 / 20-session slope: context only",
        f"Plan readiness: {readiness}",
    ]
    if missing_setup:
        lines.append(f"Missing setup: {', '.join(missing_setup)}")

    try:
        history = market_data_provider.get_history(
            Instrument(
                command.reference_symbol,
                command.name,
                AssetType.CN_ETF,
            ),
            required_history_start(
                evaluation_date=today,
                config=command.config,
            ),
            today,
            price_basis=PriceBasis.QFQ,
        )
        latest_date = _history_latest_date(history)
        evaluation = evaluate_drawdown_plan(
            history,
            command.config,
            reference_symbol=command.reference_symbol,
            expected_date=latest_date,
        )
        reached = [
            f"-{format_plan_percent(tier.drawdown)}"
            for tier in evaluation.newly_crossed_tiers
        ]
        lines.extend(
            (
                "",
                "Reference ETF data: verified as qfq daily history",
                f"Data date: {evaluation.latest_date}",
                f"Current drawdown preview: -{evaluation.drawdown:.1%}",
                "Currently reached tiers: " + (", ".join(reached) or "none"),
            )
        )
    except (MarketDataProviderError, ValueError) as exc:
        lines.extend(("", f"Reference ETF data: unavailable ({exc})"))

    try:
        nav = market_data_provider.get_fund_nav(
            Instrument(
                command.investment_fund_symbol,
                command.name,
                AssetType.CN_OPEN_FUND,
            )
        )
        lines.append(
            f"Feeder-fund NAV: verified {nav.value:.12g} on {nav.date} ({nav.source})"
        )
    except MarketDataProviderError as exc:
        lines.append(f"Feeder-fund NAV: unavailable ({exc})")

    lines.extend(
        (
            "",
            "Confirm only if these codes are the intended ETF/feeder pair and "
            "the fund follows the domestic A-share valuation calendar.",
            "This saves reminder rules only. It never places an order.",
        )
    )
    return "\n".join(lines)


def format_plan_overview(
    result: DrawdownPlanStatusResult,
    unmatched_positions: Sequence[tuple[Any, FundNav | None, str]] = (),
    dca_statuses: Sequence[Any] = (),
    profit_statuses: Sequence[Any] = (),
) -> str:
    """Format concise `/plans` output."""

    if (
        not result.statuses
        and not unmatched_positions
        and not dca_statuses
        and not profit_statuses
        and not result.no_data_skips
        and not result.errors
    ):
        return "No investment plans or positions configured."
    lines = ["📊 Investment Plans"]
    for row in profit_statuses:
        params = _load_params(str(row["params_json"]))
        thresholds = [float(value) for value in params["thresholds"]]
        lines.extend(
            (
                "",
                f"{row['name']} (Price-Gain {row['rule_id']})",
                f"Fund {row['fund_symbol']} / auto position cost",
                "Thresholds: "
                + ", ".join(format_plan_percent(value) for value in thresholds),
            )
        )
        if row["units"] is None:
            lines.append("Position: unavailable — remember /sync_position")
        elif float(row["units"]) == 0:
            lines.append("Position: closed (exact zero units)")
        elif row["position_cycle_id"] is None:
            lines.append("Position cycle unavailable — rerun /sync_position")
        else:
            accuracy = "estimated" if row["is_estimated"] else "exact"
            lines.append(
                f"Position: {accuracy}; average cost "
                f"{float(row['average_unit_cost']):.6f}; "
                f"reached {row['reached_thresholds']}/{len(thresholds)}"
            )
    for row in dca_statuses:
        params = _load_params(str(row["params_json"]))
        lines.extend(
            (
                "",
                f"{row['name']} (fixed DCA {row['rule_id']})",
                f"Fund {row['fund_symbol']} / every {params['weekday']} / "
                f"{format_plan_amount(float(params['amount']))}",
                f"Holiday policy: {params['holiday_policy']}",
            )
        )
        if row["due_date"] is None:
            lines.append("Latest occurrence: none yet")
        else:
            effective = (
                "unresolved"
                if row["effective_date"] is None
                else str(row["effective_date"])
            )
            lines.append(
                f"Latest occurrence: {row['due_date']} / {row['status']} / "
                f"NAV date {effective}"
            )
            if row["added_units"] is not None:
                lines.append(f"Estimated units added: {float(row['added_units']):.6f}")
        if row["last_synced_at"] is None:
            lines.append("Position: not synced — remember /sync_position")
        else:
            accuracy = "estimated" if row["is_estimated"] else "exact"
            lines.append(
                f"Position: {accuracy}; last sync {row['last_synced_at']}; "
                f"later estimates {row['estimates_since_sync']}"
            )
    for status in result.statuses:
        next_tier = _next_open_tier(status)
        lines.extend(
            (
                "",
                f"{status.name} (plan {status.rule_id}) — {status.readiness}",
                f"ETF {status.reference_symbol} → fund "
                f"{status.config.investment_fund_symbol}",
                f"Drawdown: -{status.evaluation.drawdown:.1%} "
                f"({status.evaluation.latest_date}, {status.evaluation.source})",
                (
                    "Next open tier: all tiers already reminded"
                    if next_tier is None
                    else "Next open tier: "
                    f"-{format_plan_percent(next_tier.drawdown)} / "
                    f"{format_plan_amount(next_tier.amount)}"
                ),
                *_format_position_lines(status),
            )
        )
    for position, nav, ownership in unmatched_positions:
        units = float(position["units"])
        accuracy = "estimated" if position["is_estimated"] else "exact"
        lines.extend(
            (
                "",
                f"Fund {position['fund_symbol']} — {ownership}",
                f"Position: {accuracy}; last sync {position['last_synced_at']}; "
                f"later estimates {position['estimates_since_sync']}",
            )
        )
        if units == 0:
            lines.append("Position value: ¥0.00 (closed)")
        elif nav is None:
            lines.append("Position value: unavailable (dated fund NAV missing)")
        else:
            lines.append(
                f"Position value: ¥{units * nav.value:,.2f} using NAV "
                f"{nav.value:.12g} on {nav.date}"
            )
    _append_plan_failures(lines, result)
    return "\n".join(lines)


def format_plan_details(result: DrawdownPlanStatusResult) -> str:
    """Format detailed read-only plan state for `/check`."""

    if not result.statuses and not result.no_data_skips and not result.errors:
        return ""
    lines = ["", "📉 Drawdown Add Plan status (read-only)"]
    for status in result.statuses:
        evaluation = status.evaluation
        lines.extend(
            (
                "",
                f"{status.name} (plan {status.rule_id})",
                f"Reference ETF: {status.reference_symbol}",
                f"Investment fund: {status.config.investment_fund_symbol}",
                f"Data: {evaluation.latest_date} / {evaluation.source} qfq close",
                f"Current: {evaluation.latest_price:.6g}",
                f"Peak: {evaluation.peak_price:.6g} on {evaluation.peak_date}",
                f"Drawdown: -{evaluation.drawdown:.1%}",
                *_format_plan_trend(status),
                f"Readiness: {status.readiness}",
            )
        )
        if status.missing_setup:
            lines.append(f"Missing setup: {', '.join(status.missing_setup)}")
        lines.append("Tiers:")
        for tier in status.config.tiers:
            if tier.key in status.added_tier_keys:
                state = "add recorded (user-confirmed)"
            elif tier.key in status.recorded_tier_keys:
                state = "reminded; no add recorded"
            else:
                state = "open"
            lines.append(
                f"• -{format_plan_percent(tier.drawdown)} / "
                f"{format_plan_amount(tier.amount)}: {state}"
            )
        next_tier = _next_open_tier(status)
        if next_tier is None:
            lines.append("Next level: all tiers already reminded")
        else:
            distance = max(0.0, next_tier.drawdown - evaluation.drawdown)
            lines.extend(
                (
                    f"Next level: -{format_plan_percent(next_tier.drawdown)}",
                    f"Distance to next level: {distance * 100:.1f} percentage points",
                )
            )
        lines.extend(_format_position_lines(status))
    _append_plan_failures(lines, result)
    return "\n".join(lines)


def _history_latest_date(history: Any) -> date:
    if history.empty or "date" not in history.columns:
        raise ValueError("Confirmed ETF history has no dated rows.")
    parsed = history["date"].dropna()
    if parsed.empty:
        raise ValueError("Confirmed ETF history has no dated rows.")
    return date.fromisoformat(str(parsed.max())[:10])


def _next_open_tier(status: DrawdownPlanStatus) -> Any | None:
    return next(
        (
            tier
            for tier in status.config.tiers
            if tier.key not in status.recorded_tier_keys
        ),
        None,
    )


def _format_plan_trend(status: DrawdownPlanStatus) -> tuple[str, ...]:
    evaluation = status.evaluation
    label = f"MA{status.config.sma_window}"
    if evaluation.sma is None:
        return (f"{label}: unavailable (insufficient history)",)
    lines = [
        f"{label}: {evaluation.sma:.6g}",
        f"Price vs {label}: {evaluation.distance_to_sma:+.1%}",
    ]
    if evaluation.sma_slope is None:
        lines.append(f"{label} slope: unavailable (insufficient history)")
    else:
        direction = "rising" if evaluation.sma_slope > 0 else "falling"
        if math.isclose(evaluation.sma_slope, 0, abs_tol=1e-12):
            direction = "flat"
        lines.append(
            f"{label} {status.config.sma_slope_window}-session slope: "
            f"{direction} ({evaluation.sma_slope:+.1%})"
        )
    return tuple(lines)


def _format_position_lines(status: DrawdownPlanStatus) -> tuple[str, ...]:
    if status.position is None:
        return (
            "Position: not synced",
            *(
                (f"Position sync required since {status.position_sync_required_since}",)
                if status.position_sync_required_since is not None
                else ()
            ),
        )
    accuracy = "estimated" if status.position["is_estimated"] else "exact"
    units = float(status.position["units"])
    lines = [
        f"Position: {accuracy}; last sync {status.position['last_synced_at']}; "
        f"later estimates {status.position['estimates_since_sync']}"
    ]
    if status.position_sync_required_since is not None:
        lines.append(
            f"Position sync required since {status.position_sync_required_since}"
        )
    if units == 0:
        lines.append("Position value: ¥0.00 (closed)")
    elif status.fund_nav is None:
        lines.append("Position value: unavailable (dated fund NAV missing)")
    else:
        lines.append(
            f"Position value: ¥{units * status.fund_nav.value:,.2f} using NAV "
            f"{status.fund_nav.value:.12g} on {status.fund_nav.date}"
        )
    return tuple(lines)


def _append_plan_failures(
    lines: list[str],
    result: DrawdownPlanStatusResult,
) -> None:
    for skip in result.no_data_skips:
        lines.append(f"⚠️ {skip.symbol}: data unavailable — {skip.message}")
    for error in result.errors:
        lines.append(f"❌ Plan {error.rule_id} {error.symbol}: {error.message}")


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
    status = "enabled" if bool(row["enabled"]) else "disabled"
    if row["type"] == DCA_RULE_TYPE:
        return (
            f"id={row['id']} type={row['type']} name={row['name']}"
            f" status={status} params={params_text}"
        )

    return (
        f"id={row['id']} "
        f"type={row['type']} "
        f"asset_type={row['asset_type']} "
        f"symbol={row['symbol']} "
        f"name={row['name']} "
        f"status={status} "
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


def _dca_skip_response(status: str, rule_id: int, due_date: str) -> str:
    if status == "skipped":
        return f"Skipped fixed DCA occurrence {rule_id} / {due_date}."
    if status == "pending":
        return "The occurrence is still pending; try again."
    if status == "applied":
        return (
            "This estimate was already applied. Use /sync_position to correct "
            "the platform position; no units were subtracted."
        )
    if status == "reconciled_by_sync":
        return "This occurrence was already reconciled by Position Sync."
    return "Fixed DCA occurrence not found."


async def _reply_text(
    update: Update,
    text: str,
    *,
    reply_markup: object | None = None,
) -> None:
    if update.effective_message is None:
        LOGGER.warning("Telegram command update has no effective message")
        return

    if reply_markup is None:
        await update.effective_message.reply_text(text)
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup)


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
    market_calendar: MarketCalendar | None = None,
    notification_settings: NotificationSettings | None = None,
    timezone: str = "Asia/Shanghai",
    now_factory: Callable[[], datetime] | None = None,
) -> list[Any]:
    """Build Telegram command handlers with an allowlist guard."""
    from telegram.ext import CallbackQueryHandler, CommandHandler

    allowed_user_ids = frozenset(allowed_user_ids)
    notification_settings = notification_settings or NotificationSettings()
    if market_data_provider is None:
        market_data_provider = AkshareMarketDataProvider()
    if market_calendar is None:
        market_calendar = CNMarketCalendar()
    plan_drafts: dict[str, DrawdownPlanDraft] = {}
    manual_add_drafts: dict[str, ManualAddDraft] = {}
    position_sync_drafts: dict[str, PositionSyncDraft] = {}
    clock = now_factory or (lambda: datetime.now(UTC))
    timezone_info = ZoneInfo(timezone)
    cn_market_timezone = ZoneInfo("Asia/Shanghai")

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
            try:
                if command.cost == "auto":
                    rule_id = add_position_profit_rule(
                        connection,
                        fund_symbol=command.symbol,
                        name=command.name,
                        thresholds=command.thresholds,
                    )
                else:
                    rule_id = add_rule(
                        connection,
                        type=PROFIT_RULE_TYPE,
                        symbol=command.symbol,
                        name=command.name,
                        asset_type=command.asset_type.value,
                        params=profit_params(command),
                    )
            except sqlite3.IntegrityError as exc:
                await _reply_text(update, str(exc))
                return

            preview = None
            preview_error = None
            if command.cost == "auto":
                try:
                    position = get_position_snapshot(connection, command.symbol)
                    cycle = get_active_position_cycle(connection, command.symbol)
                    if (
                        position is None
                        or cycle is None
                        or float(position["units"]) <= 0
                    ):
                        raise ValueError("positive Position Snapshot is missing")
                    expected_date = latest_completed_open_date(
                        market_calendar,
                        _clock_now(clock).astimezone(timezone_info).date(),
                    )
                    nav = market_data_provider.get_fund_nav(
                        Instrument(
                            command.symbol,
                            command.name,
                            AssetType.CN_OPEN_FUND,
                        ),
                        nav_date=expected_date,
                    )
                    rule = next(
                        row
                        for row in db_list_rules(connection)
                        if int(row["id"]) == rule_id
                    )
                    preview = build_position_profit_alert(
                        rule,
                        nav,
                        position,
                        position_cycle_id=int(cycle["id"]),
                        recorded_threshold_keys=set(),
                    )
                except (MarketDataProviderError, ValueError) as exc:
                    preview_error = str(exc)

        if command.cost != "auto":
            response = (
                f"Added profit rule id={rule_id} "
                f"asset_type={command.asset_type.value} "
                f"symbol={command.symbol} name={command.name} "
                f"cost={float(command.cost):.12g}"
            )
        else:
            lines = [
                f"Added auto-cost Price-Gain rule id={rule_id}",
                f"Fund: {command.symbol} / {command.name}",
                "Thresholds: "
                + ", ".join(format_plan_percent(value) for value in command.thresholds),
            ]
            if preview_error is not None:
                lines.append(f"Read-only preview unavailable: {preview_error}.")
            elif preview is None:
                lines.append("Read-only preview: no configured threshold is reached.")
            else:
                payload = preview[0]["payload"]
                reached = payload["crossed_thresholds"]
                lines.extend(
                    (
                        f"Read-only preview ({payload['accuracy']}): gain "
                        f"{float(payload['profit_rate']):+.1%} on "
                        f"{payload['nav_date']}",
                        "Currently reached: "
                        + ", ".join(
                            format_plan_percent(float(item["threshold"]))
                            for item in reached
                        ),
                        "Preview only; no threshold was consumed and no trade "
                        "occurred.",
                    )
                )
            response = "\n".join(lines)
        await _reply_text(update, response)

    async def add_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_add_dca_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        fund_type = None
        if command.fund_symbol is not None:
            metadata_reader = getattr(market_data_provider, "get_fund_type", None)
            if not callable(metadata_reader):
                await _reply_text(
                    update,
                    "The market-data provider cannot verify the fund's domestic "
                    "calendar. No fixed DCA rule was created.",
                )
                return
            try:
                fund_type = str(metadata_reader(command.fund_symbol))
            except MarketDataProviderError as exc:
                await _reply_text(
                    update,
                    "Unable to verify the fund's domestic calendar from "
                    f"metadata: {exc}. No rule was created; try again later.",
                )
                return
            if "QDII" in fund_type.upper() or "海外" in fund_type:
                await _reply_text(
                    update,
                    f"Fund type {fund_type} does not use the domestic CN "
                    "valuation calendar; no fixed DCA rule was created.",
                )
                return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            try:
                if command.fund_symbol is None:
                    rule_id = add_rule(
                        connection,
                        type=DCA_RULE_TYPE,
                        symbol=command.name,
                        name=command.name,
                        asset_type="dca",
                        params=dca_params(command),
                    )
                else:
                    rule_id = add_enhanced_dca_rule(
                        connection,
                        fund_symbol=command.fund_symbol,
                        name=command.name,
                        weekday=command.weekday,
                        amount=command.amount,
                        fee_mode=str(command.fee_mode),
                        fee_value=float(command.fee_value),
                        holiday_policy=str(command.holiday_policy),
                    )
            except sqlite3.IntegrityError as exc:
                await _reply_text(update, str(exc))
                return

        if command.fund_symbol is None:
            response = (
                f"Added DCA rule id={rule_id} "
                f"name={command.name} weekday={command.weekday} "
                f"amount={command.amount}"
            )
        else:
            fee = (
                f"rate:{format_plan_percent(float(command.fee_value))}"
                if command.fee_mode == "rate"
                else f"fixed:{format_plan_amount(float(command.fee_value))}"
            )
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                settings = get_fund_settings(connection, command.fund_symbol)
                position = get_position_snapshot(connection, command.fund_symbol)
            response = "\n".join(
                (
                    f"Added fixed DCA rule id={rule_id}",
                    f"Fund: {command.fund_symbol} / {command.name}",
                    f"Verified fund type: {fund_type}",
                    f"Weekly due day: {command.weekday}",
                    f"Gross amount: {format_plan_amount(command.amount)}",
                    f"Shared subscription fee: {fee}",
                    f"Holiday policy: {command.holiday_policy}",
                    f"Subscription cutoff: {settings['subscription_cutoff']}",
                    "Position readiness: "
                    + ("READY" if position is not None else "SETUP_REQUIRED"),
                    "Remember: run /sync_position before automatic estimates "
                    "can apply.",
                )
            )
        await _reply_text(update, response)

    async def add_drawdown_plan(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_add_drawdown_plan_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        user_id = get_update_user_id(update)
        chat_id = get_update_chat_id(update)
        if user_id is None or chat_id is None:
            await _reply_text(
                update, "Unable to scope the confirmation; rerun command."
            )
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            conflict = find_enabled_drawdown_plan_conflict(
                connection,
                reference_symbol=command.reference_symbol,
                investment_fund_symbol=command.investment_fund_symbol,
            )
            if conflict is not None:
                await _reply_text(
                    update,
                    f"Plan conflict: enabled plan id={conflict['id']} already uses "
                    "this Reference ETF or Investment Feeder Fund.",
                )
                return
            preview = build_drawdown_plan_preview(
                connection,
                market_data_provider,
                command,
                today=_clock_now(clock).astimezone(timezone_info).date(),
            )

        now = _clock_now(clock)
        for old_token, draft in tuple(plan_drafts.items()):
            if draft.expires_at <= now:
                plan_drafts.pop(old_token, None)
        token = secrets.token_urlsafe(9)
        plan_drafts[token] = DrawdownPlanDraft(
            user_id=user_id,
            chat_id=chat_id,
            expires_at=now + timedelta(minutes=10),
            command=command,
        )
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Confirm pair + domestic calendar",
                        callback_data=f"drawdown_plan_confirm:{token}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Cancel",
                        callback_data=f"drawdown_plan_cancel:{token}",
                    )
                ],
            ]
        )
        await _reply_text(update, preview, reply_markup=markup)

    async def drawdown_plan_draft_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        data = str(getattr(query, "data", ""))
        action, separator, token = data.partition(":")
        if not separator or action not in {
            "drawdown_plan_confirm",
            "drawdown_plan_cancel",
        }:
            return
        draft = plan_drafts.get(token)
        now = _clock_now(clock)
        if (
            draft is None
            or draft.expires_at <= now
            or draft.user_id != get_update_user_id(update)
            or draft.chat_id != get_update_chat_id(update)
        ):
            await query.edit_message_text(
                "This plan confirmation expired or belongs to another chat. "
                "Rerun /add_drawdown_plan."
            )
            return
        if action == "drawdown_plan_cancel":
            plan_drafts.pop(token, None)
            await query.edit_message_text("Drawdown Add Plan creation cancelled.")
            return
        if draft.created_rule_id is None:
            try:
                with open_connection(sqlite_path) as connection:
                    initialize_database(connection)
                    draft.created_rule_id = add_drawdown_plan_rule(
                        connection,
                        reference_symbol=draft.command.reference_symbol,
                        investment_fund_symbol=(draft.command.investment_fund_symbol),
                        name=draft.command.name,
                        params=draft.command.params,
                    )
            except sqlite3.IntegrityError as exc:
                await query.edit_message_text(f"Plan was not saved: {exc}")
                return
        await query.edit_message_text(
            f"Saved Drawdown Add Plan id={draft.created_rule_id}: "
            f"ETF {draft.command.reference_symbol} → fund "
            f"{draft.command.investment_fund_symbol}. The first scheduled "
            "confirmed-close evaluation will initialize its cycle. No order "
            "has been placed."
        )

    def store_manual_add_draft(
        *,
        user_id: int,
        chat_id: int,
        selection: ManualAddSelection,
    ) -> tuple[str, ManualAddDraft]:
        now = _clock_now(clock)
        for old_token, old_draft in tuple(manual_add_drafts.items()):
            if old_draft.expires_at <= now:
                manual_add_drafts.pop(old_token, None)
        token = secrets.token_urlsafe(9)
        draft = ManualAddDraft(
            user_id=user_id,
            chat_id=chat_id,
            expires_at=now + timedelta(minutes=10),
            selection=selection,
        )
        manual_add_drafts[token] = draft
        return token, draft

    def manual_add_confirmation_markup(token: str, readiness: str) -> Any:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if readiness == "READY":
            rows = [
                [
                    InlineKeyboardButton(
                        "实际金额与配置总额完全一致",
                        callback_data=f"drawdown_add_confirm:{token}:match",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "金额不同，记录档位后同步持仓",
                        callback_data=f"drawdown_add_confirm:{token}:sync",
                    )
                ],
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton(
                        "记录档位，稍后同步持仓",
                        callback_data=f"drawdown_add_confirm:{token}:sync",
                    )
                ]
            ]
        rows.append(
            [
                InlineKeyboardButton(
                    "取消",
                    callback_data=f"drawdown_add_confirm:{token}:cancel",
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    async def mark_added(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_mark_added_args(getattr(context, "args", ()))
            action_date = _clock_now(clock).astimezone(cn_market_timezone).date()
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                selection = load_manual_add_selection(
                    connection,
                    plan_id=command.plan_id,
                    action_date=action_date,
                    tier_keys=command.tier_keys,
                )
        except (CommandParseError, ValueError) as exc:
            await _reply_text(update, str(exc))
            return
        user_id = get_update_user_id(update)
        chat_id = get_update_chat_id(update)
        if user_id is None or chat_id is None:
            await _reply_text(
                update, "Unable to scope the confirmation; rerun command."
            )
            return
        token, _draft = store_manual_add_draft(
            user_id=user_id,
            chat_id=chat_id,
            selection=selection,
        )
        await _reply_text(
            update,
            format_manual_add_confirmation(selection),
            reply_markup=manual_add_confirmation_markup(token, selection.readiness),
        )

    async def manual_add_event_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        parts = str(getattr(query, "data", "")).split(":")
        if len(parts) not in {4, 5} or parts[0] != "drawdown_add":
            return
        try:
            plan_id = int(parts[1])
            event_id = int(parts[2])
            tier_index = (
                int(parts[4]) if parts[3] == "tier" and len(parts) == 5 else None
            )
        except ValueError:
            return
        selector = parts[3]
        if selector == "none":
            await query.edit_message_text(
                "No addition recorded. The tier reminder state was not changed. "
                "No order has been placed."
            )
            return
        user_id = get_update_user_id(update)
        chat_id = get_update_chat_id(update)
        if user_id is None or chat_id is None:
            await query.edit_message_text("Unable to scope this action.")
            return
        try:
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                selection = load_manual_add_selection(
                    connection,
                    plan_id=plan_id,
                    event_id=event_id,
                    action_date=_clock_now(clock).astimezone(cn_market_timezone).date(),
                    select_all=selector == "all",
                    tier_index=tier_index,
                )
        except (CommandParseError, ValueError) as exc:
            await query.edit_message_text(str(exc))
            return
        token, _draft = store_manual_add_draft(
            user_id=user_id,
            chat_id=chat_id,
            selection=selection,
        )
        await query.edit_message_text(
            format_manual_add_confirmation(selection),
            reply_markup=manual_add_confirmation_markup(token, selection.readiness),
        )

    def get_manual_add_draft(update: Update, token: str) -> ManualAddDraft | None:
        draft = manual_add_drafts.get(token)
        now = _clock_now(clock)
        if (
            draft is None
            or draft.expires_at <= now
            or draft.user_id != get_update_user_id(update)
            or draft.chat_id != get_update_chat_id(update)
        ):
            return None
        return draft

    async def commit_manual_add(
        query: Any,
        draft: ManualAddDraft,
        *,
        create_estimate: bool,
        cutoff_choice: str | None = None,
    ) -> None:
        if draft.completed_message is not None:
            await query.edit_message_text(draft.completed_message)
            return
        now = _clock_now(clock).astimezone(cn_market_timezone)
        if now.date() != draft.selection.market_date:
            await query.edit_message_text(
                "This reminder expired after its market date. Use /sync_position "
                "after the fund platform updates."
            )
            return
        effective_date = None
        try:
            if create_estimate:
                if not market_calendar.confirmed_status(now.date()):
                    raise ValueError("The action date is not a confirmed CN open day.")
                effective_date = (
                    _next_confirmed_trading_day(market_calendar, now.date())
                    if cutoff_choice == "after"
                    else now.date()
                )
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                estimate_id, recorded_keys = record_manual_addition(
                    connection,
                    rule_id=draft.selection.plan_id,
                    cycle_id=draft.selection.cycle_id,
                    source_alert_event_id=draft.selection.event_id,
                    fund_symbol=draft.selection.fund_symbol,
                    tiers=draft.selection.tiers,
                    action_at=now,
                    create_estimate=create_estimate,
                    cutoff_time=(draft.selection.cutoff if create_estimate else None),
                    cutoff_choice=cutoff_choice,
                    effective_date=(
                        None if effective_date is None else effective_date.isoformat()
                    ),
                )
        except (MarketDataProviderError, ValueError, sqlite3.IntegrityError) as exc:
            await query.edit_message_text(f"Addition was not recorded: {exc}")
            return
        tier_text = ", ".join(
            f"-{format_plan_percent(float(key))}" for key in recorded_keys
        )
        if not recorded_keys:
            message = "These tiers were already recorded; no duplicate was created."
        elif estimate_id is None:
            message = (
                f"Recorded tiers {tier_text}. Position sync is required. After the "
                "platform settles, run /sync_position with current units and average "
                "cost. No order has been placed."
            )
        else:
            message = (
                f"Recorded tiers {tier_text}; waiting for exact dated NAV on "
                f"{effective_date}. Estimate id={estimate_id}. The bot did not place "
                "or verify an order."
            )
        draft.completed_message = message
        await query.edit_message_text(message)

    async def dca_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        args = getattr(context, "args", ())
        if len(args) != 2:
            await _reply_text(update, "Usage: /dca_skip <rule_id> <due_date>")
            return
        try:
            rule_id = int(args[0])
            due_date = date.fromisoformat(args[1]).isoformat()
        except ValueError:
            await _reply_text(update, "rule_id must be an integer and date YYYY-MM-DD")
            return
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            status = skip_scheduled_dca_occurrence(
                connection,
                rule_id=rule_id,
                due_date=due_date,
            )
        await _reply_text(update, _dca_skip_response(status, rule_id, due_date))

    async def dca_skip_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            _prefix, raw_rule_id, raw_due_date = str(query.data).split(":", 2)
            rule_id = int(raw_rule_id)
            due_date = date.fromisoformat(raw_due_date).isoformat()
        except (TypeError, ValueError):
            await query.edit_message_text("Invalid DCA skip action.")
            return
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            status = skip_scheduled_dca_occurrence(
                connection,
                rule_id=rule_id,
                due_date=due_date,
            )
        await query.edit_message_text(_dca_skip_response(status, rule_id, due_date))

    async def manual_add_confirm_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        parts = str(getattr(query, "data", "")).split(":")
        if len(parts) != 3:
            return
        token, action = parts[1], parts[2]
        draft = get_manual_add_draft(update, token)
        if draft is None:
            await query.edit_message_text(
                "This addition confirmation expired or belongs to another chat. "
                "Rerun /mark_added."
            )
            return
        if action == "cancel":
            manual_add_drafts.pop(token, None)
            await query.edit_message_text("Manual addition recording cancelled.")
            return
        if action == "sync":
            await commit_manual_add(query, draft, create_estimate=False)
            return
        if action != "match" or draft.selection.readiness != "READY":
            return
        cn_now = _clock_now(clock).astimezone(cn_market_timezone)
        cutoff = time.fromisoformat(draft.selection.cutoff)
        if cn_now.timetz().replace(tzinfo=None) < cutoff:
            await commit_manual_add(
                query,
                draft,
                create_estimate=True,
                cutoff_choice="before",
            )
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        await query.edit_message_text(
            "The confirmation is at or after the configured cutoff. When did you "
            "actually submit the fund subscription?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"{draft.selection.cutoff}前已提交 — 当日净值",
                            callback_data=f"drawdown_add_cutoff:{token}:before",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            f"{draft.selection.cutoff}后才提交 — 下一开放日",
                            callback_data=f"drawdown_add_cutoff:{token}:after",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "取消",
                            callback_data=f"drawdown_add_cutoff:{token}:cancel",
                        )
                    ],
                ]
            ),
        )

    async def manual_add_cutoff_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        parts = str(getattr(query, "data", "")).split(":")
        if len(parts) != 3:
            return
        token, choice = parts[1], parts[2]
        draft = get_manual_add_draft(update, token)
        if draft is None:
            await query.edit_message_text("This cutoff confirmation expired.")
            return
        if choice == "cancel":
            manual_add_drafts.pop(token, None)
            await query.edit_message_text("Manual addition recording cancelled.")
            return
        if choice not in {"before", "after"}:
            return
        await commit_manual_add(
            query,
            draft,
            create_estimate=True,
            cutoff_choice=choice,
        )

    async def set_fund_fee(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_set_fund_fee_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            row = upsert_fund_fee(
                connection,
                fund_symbol=command.fund_symbol,
                fee_mode=command.fee_mode,
                fee_value=command.fee_value,
            )
        await _reply_text(update, format_fund_fee(row))

    async def set_fund_cutoff(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_set_fund_cutoff_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            row = upsert_fund_cutoff(
                connection,
                fund_symbol=command.fund_symbol,
                subscription_cutoff=command.subscription_cutoff,
            )
        await _reply_text(
            update,
            (
                f"Updated fund {row['fund_symbol']} subscription cutoff to "
                f"{row['subscription_cutoff']}. The change applies only to future "
                "manual confirmations."
            ),
        )

    async def sync_position(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        try:
            command = parse_sync_position_args(getattr(context, "args", ()))
        except CommandParseError as exc:
            await _reply_text(update, str(exc))
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            pending_items = list_pending_position_items(
                connection,
                command.fund_symbol,
            )
            if not pending_items:
                row = upsert_position_snapshot(
                    connection,
                    fund_symbol=command.fund_symbol,
                    units=command.units,
                    average_unit_cost=command.average_unit_cost,
                )
            else:
                row = None

        if pending_items:
            user_id = get_update_user_id(update)
            chat_id = get_update_chat_id(update)
            if user_id is None or chat_id is None:
                await _reply_text(update, "Unable to scope the position confirmation.")
                return
            now = _clock_now(clock)
            token = secrets.token_urlsafe(9)
            position_sync_drafts[token] = PositionSyncDraft(
                user_id=user_id,
                chat_id=chat_id,
                expires_at=now + timedelta(minutes=10),
                command=command,
                item_keys=tuple(str(item["key"]) for item in pending_items),
            )
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            instructions = [
                "Pending additions exist. Classify this platform snapshot:",
                "Choose All only if every item below is included. Choose None only "
                "if no item below is included. If it includes some items, cancel and "
                "sync again after all items settle.",
            ]
            item_lines = [
                f"• {item['kind']} / {item['date']} / "
                f"{format_plan_amount(float(item['amount']))}"
                for item in pending_items
            ]
            preview = "\n".join((*instructions, "", *item_lines))
            if len(preview) > 4096:
                pages: list[list[str]] = [[]]
                for line in item_lines:
                    candidate = "\n".join((*pages[-1], line))
                    if pages[-1] and len(candidate) > 3800:
                        pages.append([line])
                    else:
                        pages[-1].append(line)
                for index, page in enumerate(pages, start=1):
                    await _reply_text(
                        update,
                        f"Pending additions ({index}/{len(pages)}):\n\n"
                        + "\n".join(page),
                    )
                preview = "\n".join(
                    (
                        *instructions,
                        "",
                        f"Review all {len(item_lines)} pending additions listed "
                        "above before choosing.",
                    )
                )
            await _reply_text(
                update,
                preview,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "已全部包含",
                                callback_data=f"position_sync:{token}:included",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "均未包含",
                                callback_data=f"position_sync:{token}:none_included",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "只包含部分（取消）",
                                callback_data=f"position_sync:{token}:partial",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "取消",
                                callback_data=f"position_sync:{token}:cancel",
                            )
                        ],
                    ]
                ),
            )
            return

        nav = None
        if command.units > 0:
            try:
                nav = market_data_provider.get_fund_nav(
                    Instrument(
                        command.fund_symbol,
                        command.fund_symbol,
                        AssetType.CN_OPEN_FUND,
                    )
                )
            except MarketDataProviderError:
                LOGGER.warning(
                    "Unable to fetch latest feeder-fund NAV symbol=%s",
                    command.fund_symbol,
                )
        await _reply_text(update, format_position_snapshot(row, nav))

    async def position_sync_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        parts = str(getattr(query, "data", "")).split(":")
        if len(parts) != 3:
            return
        token, choice = parts[1], parts[2]
        draft = position_sync_drafts.get(token)
        now = _clock_now(clock)
        if (
            draft is None
            or draft.expires_at <= now
            or draft.user_id != get_update_user_id(update)
            or draft.chat_id != get_update_chat_id(update)
        ):
            await query.edit_message_text(
                "This position confirmation expired. Rerun /sync_position."
            )
            return
        if choice in {"cancel", "partial"}:
            position_sync_drafts.pop(token, None)
            message = "Position sync cancelled."
            if choice == "partial":
                message += (
                    " This snapshot includes only some pending additions; rerun "
                    "/sync_position after all listed additions settle. Nothing was "
                    "changed."
                )
            await query.edit_message_text(message)
            return
        if choice not in {"included", "none_included"}:
            return
        if draft.completed_message is not None:
            await query.edit_message_text(draft.completed_message)
            return
        try:
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                row = reconcile_position_snapshot(
                    connection,
                    fund_symbol=draft.command.fund_symbol,
                    units=draft.command.units,
                    average_unit_cost=draft.command.average_unit_cost,
                    expected_item_keys=draft.item_keys,
                    all_included=choice == "included",
                    synced_at=now,
                )
        except sqlite3.IntegrityError as exc:
            await query.edit_message_text(str(exc))
            return
        message = format_position_snapshot(row, None)
        if choice == "none_included":
            if any(key.startswith("estimate:") for key in draft.item_keys):
                message += (
                    "\nPending estimates remain eligible for dated-NAV processing."
                )
            if any(key.startswith("action:") for key in draft.item_keys):
                message += (
                    "\nUnestimated manual additions still require another "
                    "/sync_position after the platform units settle."
                )
            if any(key.startswith("dca:") for key in draft.item_keys):
                message += (
                    "\nPending fixed DCA estimates remain eligible for NAV processing."
                )
        draft.completed_message = message
        await query.edit_message_text(message)

    async def position_profit_action_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        parts = str(getattr(query, "data", "")).split(":")
        if len(parts) != 3:
            return
        try:
            event_id = int(parts[1])
        except ValueError:
            return
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            event = get_position_profit_event(connection, event_id)
        if event is None:
            await query.edit_message_text("Price-Gain reminder was not found.")
            return
        choice = parts[2]
        if choice == "partial":
            await query.edit_message_text(
                "After the platform confirms the redemption, run:\n"
                f"/sync_position {event['symbol']} <remaining_units> "
                "<new_average_unit_cost>\n\nNothing was changed by this button."
            )
            return
        if choice == "none":
            await query.edit_message_text(
                "Recorded: no position update. Nothing was changed and no trade "
                "was placed."
            )
            return
        if choice != "close":
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        await query.edit_message_text(
            "Confirm only after the platform shows zero units. This will set the "
            f"tracked position for {event['symbol']} to 0 and close this position "
            "cycle.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Confirm zero position",
                            callback_data=f"profit_close_confirm:{event_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "Cancel",
                            callback_data=f"profit_close_cancel:{event_id}",
                        )
                    ],
                ]
            ),
        )

    async def position_profit_close_callback(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        del context
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        await query.answer()
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        parts = str(getattr(query, "data", "")).split(":")
        if len(parts) != 2:
            return
        try:
            event_id = int(parts[1])
        except ValueError:
            return
        if str(query.data).startswith("profit_close_cancel:"):
            await query.edit_message_text("Position close cancelled. Nothing changed.")
            return
        try:
            with open_connection(sqlite_path) as connection:
                initialize_database(connection)
                changed = close_position_from_profit_event(
                    connection,
                    event_id=event_id,
                    synced_at=_clock_now(clock),
                )
        except sqlite3.IntegrityError as exc:
            await query.edit_message_text(str(exc))
            return
        await query.edit_message_text(
            "Tracked position set to zero; position cycle closed."
            if changed
            else "Position was already zero; nothing changed."
        )

    async def list_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if await reject_if_unauthorized(update, allowed_user_ids):
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            response = format_rules_list(db_list_rules(connection))

        await _reply_text(update, response)

    async def plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if await reject_if_unauthorized(update, allowed_user_ids):
            return
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            result = read_drawdown_plan_statuses(
                connection,
                market_data_provider,
                end_date=_clock_now(clock).astimezone(timezone_info).date(),
            )
            planned_funds = set(list_enabled_drawdown_plan_fund_symbols(connection))
            dca_statuses = list_enhanced_dca_statuses(connection)
            profit_statuses = list_position_profit_statuses(connection)
            dca_funds = {str(row["fund_symbol"]) for row in dca_statuses}
            profit_funds = {str(row["fund_symbol"]) for row in profit_statuses}
            evaluated_funds = (
                {status.config.investment_fund_symbol for status in result.statuses}
                | dca_funds
                | profit_funds
            )
            unmatched_rows = [
                row
                for row in list_position_snapshots(connection)
                if row["fund_symbol"] not in evaluated_funds
            ]
            unmatched_positions = []
            for row in unmatched_rows:
                nav = None
                if float(row["units"]) > 0:
                    try:
                        nav = market_data_provider.get_fund_nav(
                            Instrument(
                                str(row["fund_symbol"]),
                                str(row["fund_symbol"]),
                                AssetType.CN_OPEN_FUND,
                            )
                        )
                    except MarketDataProviderError:
                        LOGGER.warning(
                            "Unable to fetch standalone position NAV symbol=%s",
                            row["fund_symbol"],
                        )
                ownership = (
                    "Drawdown Add Plan configured; market status unavailable"
                    if row["fund_symbol"] in planned_funds
                    else "no enabled Drawdown Add Plan"
                )
                unmatched_positions.append((row, nav, ownership))
        await _reply_text(
            update,
            format_plan_overview(
                result,
                unmatched_positions,
                dca_statuses,
                profit_statuses,
            ),
        )

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
            rule_identity = next(
                (
                    (
                        str(row["type"]),
                        str(row["asset_type"]),
                        _load_params(str(row["params_json"])).get("cost"),
                    )
                    for row in db_list_rules(connection)
                    if int(row["id"]) == rule_id
                ),
                None,
            )
            removed = delete_rule(connection, rule_id)

        if removed and rule_identity == ("drawdown_plan", "cn_etf", None):
            await _reply_text(update, f"Disabled drawdown plan id={rule_id}")
        elif removed and rule_identity == (DCA_RULE_TYPE, "cn_open_fund", None):
            await _reply_text(update, f"Disabled fixed DCA rule id={rule_id}")
        elif removed and rule_identity == (
            PROFIT_RULE_TYPE,
            "cn_open_fund",
            "auto",
        ):
            await _reply_text(
                update, f"Disabled auto-cost Price-Gain rule id={rule_id}"
            )
        elif removed:
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
            dca_result = evaluate_dca_rules(
                connection,
                market_calendar=market_calendar,
            )
            plan_status_result = read_drawdown_plan_statuses(
                connection,
                market_data_provider,
                end_date=_clock_now(clock).astimezone(timezone_info).date(),
            )

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
        legacy_checked = (
            result.checked_rules
            + dca_result.checked_rules
            + profit_result.checked_rules
        )
        if legacy_checked == 0 and (
            plan_status_result.statuses
            or plan_status_result.no_data_skips
            or plan_status_result.errors
        ):
            response = (
                "📋 Check summary\n\n"
                f"✅ Read-only Drawdown Add Plans checked: "
                f"{plan_status_result.checked_rules}."
            )
        response += format_plan_details(plan_status_result)
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
        CommandHandler("add_drawdown_plan", add_drawdown_plan),
        CommandHandler("mark_added", mark_added),
        CommandHandler("set_fund_fee", set_fund_fee),
        CommandHandler("set_fund_cutoff", set_fund_cutoff),
        CommandHandler("sync_position", sync_position),
        CommandHandler("dca_skip", dca_skip),
        CommandHandler("list", list_rules),
        CommandHandler("plans", plans),
        CommandHandler("del", delete_rule_command),
        CommandHandler("check", check),
        CallbackQueryHandler(
            drawdown_plan_draft_callback,
            pattern=r"^drawdown_plan_(?:confirm|cancel):",
        ),
        CallbackQueryHandler(
            manual_add_event_callback,
            pattern=(r"^drawdown_add:[0-9]+:[0-9]+:(?:all|none|tier:[0-9]+)$"),
        ),
        CallbackQueryHandler(
            manual_add_confirm_callback,
            pattern=r"^drawdown_add_confirm:",
        ),
        CallbackQueryHandler(
            manual_add_cutoff_callback,
            pattern=r"^drawdown_add_cutoff:",
        ),
        CallbackQueryHandler(
            position_sync_callback,
            pattern=r"^position_sync:",
        ),
        CallbackQueryHandler(dca_skip_callback, pattern=r"^dca_skip:"),
        CallbackQueryHandler(
            position_profit_action_callback,
            pattern=r"^profit_action:[0-9]+:(?:partial|close|none)$",
        ),
        CallbackQueryHandler(
            position_profit_close_callback,
            pattern=r"^profit_close_(?:confirm|cancel):[0-9]+$",
        ),
        CommandHandler("test_notify", test_notify),
    ]


def register_command_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    allowed_user_ids: Collection[int],
    *,
    sqlite_path: str | Path = ":memory:",
    market_data_provider: MarketDataProvider | None = None,
    market_calendar: MarketCalendar | None = None,
    notification_settings: NotificationSettings | None = None,
    timezone: str = "Asia/Shanghai",
) -> None:
    """Register supported Telegram command handlers."""
    for handler in build_command_handlers(
        allowed_user_ids,
        sqlite_path=sqlite_path,
        market_data_provider=market_data_provider,
        market_calendar=market_calendar,
        notification_settings=notification_settings,
        timezone=timezone,
    ):
        application.add_handler(handler)


def create_application(
    *,
    token: str,
    allowed_user_ids: Collection[int],
    sqlite_path: str | Path = ":memory:",
    market_data_provider: MarketDataProvider | None = None,
    market_calendar: MarketCalendar | None = None,
    notification_settings: NotificationSettings | None = None,
    timezone: str = "Asia/Shanghai",
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
        market_calendar=market_calendar,
        notification_settings=notification_settings,
        timezone=timezone,
    )
    return application


def _command_chat_ids(update: object) -> frozenset[int]:
    chat_id = get_update_chat_id(update)
    if chat_id is None:
        LOGGER.warning("Telegram command update has no effective chat")
        return frozenset()
    return frozenset({chat_id})


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now_factory must return a timezone-aware datetime")
    return now.astimezone(UTC)


def _next_confirmed_trading_day(
    market_calendar: MarketCalendar,
    check_date: date,
) -> date:
    for days_forward in range(1, 367):
        candidate = check_date + timedelta(days=days_forward)
        if market_calendar.confirmed_status(candidate):
            return candidate
    raise ValueError(
        f"No next confirmed CN trading day found after {check_date.isoformat()}."
    )
