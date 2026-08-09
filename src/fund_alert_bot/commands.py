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
from datetime import UTC, date, datetime, timedelta
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
    read_drawdown_plan_statuses,
)
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import (
    add_drawdown_plan_rule,
    add_rule,
    delete_rule,
    find_enabled_drawdown_plan_conflict,
    initialize_database,
    list_position_snapshots,
    open_connection,
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
    FundNav,
    Instrument,
    MarketDataProvider,
    MarketDataProviderError,
    PriceBasis,
)
from fund_alert_bot.notifications.dispatch import send_alert_notifications
from fund_alert_bot.notifications.service import build_notification_service
from fund_alert_bot.rules.dca import normalize_weekday
from fund_alert_bot.rules.drawdown_plan import (
    DrawdownPlanConfig,
    evaluate_drawdown_plan,
    parse_drawdown_plan_config,
    required_history_start,
)

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

LOGGER = logging.getLogger(__name__)

ADD_DRAWDOWN_USAGE = (
    "Usage: /add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>"
)
ADD_DCA_USAGE = "Usage: /add_dca <name> <weekday> <amount>"
ADD_PROFIT_USAGE = "Usage: /add_profit <asset_type> <symbol> <name> <cost> <thresholds>"
SET_FUND_FEE_USAGE = "Usage: /set_fund_fee <fund_symbol> <rate:<percent>%|fixed:<RMB>>"
SET_FUND_CUTOFF_USAGE = "Usage: /set_fund_cutoff <fund_symbol> <HH:MM>"
SYNC_POSITION_USAGE = "Usage: /sync_position <fund_symbol> <units> <average_unit_cost>"
ADD_DRAWDOWN_PLAN_USAGE = (
    "Usage: /add_drawdown_plan <reference_etf_symbol> <feeder_fund_symbol> "
    "<name> <tiers> [lookback:<calendar_days>]"
)
START_MESSAGE = "fund-alert-bot is running. Use /help to see available commands."
HELP_MESSAGE = "\n".join(
    (
        "Available commands:",
        "/start - Start the bot",
        "/help - Show available commands",
        "/add_drawdown <asset_type> <symbol> <name> <lookback_days> <thresholds>",
        "/add_profit <asset_type> <symbol> <name> <cost> <thresholds>",
        "/add_dca <name> <weekday> <amount>",
        "/set_fund_fee <fund_symbol> <rate:<percent>%|fixed:<RMB>>",
        "/set_fund_cutoff <fund_symbol> <HH:MM>",
        "/sync_position <fund_symbol> <units> <average_unit_cost>",
        "/add_drawdown_plan <reference_etf> <feeder_fund> <name> <tiers> "
        "[lookback:<days>]",
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


@dataclass(frozen=True, slots=True)
class ProfitCommand:
    """Parsed /add_profit command fields."""

    asset_type: AssetType
    symbol: str
    name: str
    cost: float
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


def parse_set_fund_fee_args(args: Sequence[str]) -> FundFeeCommand:
    """Parse a shared feeder-fund subscription fee."""

    if len(args) != 2:
        raise CommandParseError(SET_FUND_FEE_USAGE)
    fund_symbol = _parse_fund_symbol(args[0])
    raw_fee = args[1].strip().lower()
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
    return FundFeeCommand(fund_symbol, fee_mode, value / divisor)


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
            f"-{tier.drawdown:.0%} → ¥{tier.amount:,.2f}"
            for tier in command.config.tiers
        ),
        "Maximum one-cycle total: "
        f"¥{sum(tier.amount for tier in command.config.tiers):,.2f}",
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
        reached = [f"-{tier.drawdown:.0%}" for tier in evaluation.newly_crossed_tiers]
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
    standalone_positions: Sequence[tuple[Any, FundNav | None]] = (),
) -> str:
    """Format concise `/plans` output."""

    if (
        not result.statuses
        and not standalone_positions
        and not result.no_data_skips
        and not result.errors
    ):
        return "No investment plans or positions configured."
    lines = ["📊 Investment Plans"]
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
                    else f"Next open tier: -{next_tier.drawdown:.0%} / "
                    f"¥{next_tier.amount:,.2f}"
                ),
                *_format_position_lines(status),
            )
        )
    for position, nav in standalone_positions:
        units = float(position["units"])
        accuracy = "estimated" if position["is_estimated"] else "exact"
        lines.extend(
            (
                "",
                f"Fund {position['fund_symbol']} — no Drawdown Add Plan",
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
            state = (
                "reminded; no add recorded"
                if tier.key in status.recorded_tier_keys
                else "open"
            )
            lines.append(f"• -{tier.drawdown:.0%} / ¥{tier.amount:,.2f}: {state}")
        next_tier = _next_open_tier(status)
        if next_tier is None:
            lines.append("Next level: all tiers already reminded")
        else:
            distance = max(0.0, next_tier.drawdown - evaluation.drawdown)
            lines.extend(
                (
                    f"Next level: -{next_tier.drawdown:.0%}",
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
        return ("Position: not synced",)
    accuracy = "estimated" if status.position["is_estimated"] else "exact"
    units = float(status.position["units"])
    lines = [
        f"Position: {accuracy}; last sync {status.position['last_synced_at']}; "
        f"later estimates {status.position['estimates_since_sync']}"
    ]
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
    plan_drafts: dict[str, DrawdownPlanDraft] = {}
    clock = now_factory or (lambda: datetime.now(UTC))
    timezone_info = ZoneInfo(timezone)

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
            row = upsert_position_snapshot(
                connection,
                fund_symbol=command.fund_symbol,
                units=command.units,
                average_unit_cost=command.average_unit_cost,
            )

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
            planned_funds = {
                status.config.investment_fund_symbol for status in result.statuses
            }
            standalone_rows = [
                row
                for row in list_position_snapshots(connection)
                if row["fund_symbol"] not in planned_funds
            ]
            standalone_positions = []
            for row in standalone_rows:
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
                standalone_positions.append((row, nav))
        await _reply_text(
            update,
            format_plan_overview(result, standalone_positions),
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
            rule_type = next(
                (
                    str(row["type"])
                    for row in db_list_rules(connection)
                    if int(row["id"]) == rule_id
                ),
                None,
            )
            removed = delete_rule(connection, rule_id)

        if removed and rule_type == "drawdown_plan":
            await _reply_text(update, f"Disabled drawdown plan id={rule_id}")
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
            dca_result = evaluate_dca_rules(connection)
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
        CommandHandler("set_fund_fee", set_fund_fee),
        CommandHandler("set_fund_cutoff", set_fund_cutoff),
        CommandHandler("sync_position", sync_position),
        CommandHandler("list", list_rules),
        CommandHandler("plans", plans),
        CommandHandler("del", delete_rule_command),
        CommandHandler("check", check),
        CallbackQueryHandler(
            drawdown_plan_draft_callback,
            pattern=r"^drawdown_plan_(?:confirm|cancel):",
        ),
        CommandHandler("test_notify", test_notify),
    ]


def register_command_handlers(
    application: Application[Any, Any, Any, Any, Any, Any],
    allowed_user_ids: Collection[int],
    *,
    sqlite_path: str | Path = ":memory:",
    market_data_provider: MarketDataProvider | None = None,
    notification_settings: NotificationSettings | None = None,
    timezone: str = "Asia/Shanghai",
) -> None:
    """Register supported Telegram command handlers."""
    for handler in build_command_handlers(
        allowed_user_ids,
        sqlite_path=sqlite_path,
        market_data_provider=market_data_provider,
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
