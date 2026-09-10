"""Command argument contracts and parsing, independent of Telegram handlers."""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from fund_alert_bot.market_data.models import AssetType
from fund_alert_bot.rules.dca import normalize_weekday
from fund_alert_bot.rules.drawdown_plan import (
    DEFAULT_REARM_MARGIN,
    DrawdownPlanConfig,
    parse_drawdown_plan_config,
    validate_drawdown_plan_notification_size,
)
from fund_alert_bot.rules.profit import (
    format_profit_threshold_key,
    validate_position_profit_notification_size,
)

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


SET_DCA_AMOUNT_USAGE = "Usage: /set_dca_amount <rule_id> <new_amount>"


ADD_PROFIT_USAGE = (
    "Usage: /add_profit <asset_type> <symbol> <name> <cost|auto> <thresholds>"
)


SET_FUND_FEE_USAGE = "Usage: /set_fund_fee <fund_symbol> <rate:<percent>%|fixed:<RMB>>"


SET_FUND_CUTOFF_USAGE = "Usage: /set_fund_cutoff <fund_symbol> <HH:MM>"


SYNC_POSITION_USAGE = "Usage: /sync_position <fund_symbol> <units> <average_unit_cost>"


ADD_DRAWDOWN_PLAN_USAGE = (
    "Usage: /add_drawdown_plan <reference_etf_symbol> <feeder_fund_symbol> "
    "<name> <tiers> [lookback:<calendar_days>] [rearm:<percent>]"
)


SET_PLAN_REARM_USAGE = "Usage: /set_plan_rearm <plan_id> <percent>"


MARK_ADDED_USAGE = "Usage: /mark_added <plan_id> <tier_percentages> [YYYY-MM-DD]"


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


@dataclass(frozen=True, slots=True)
class SetPlanRearmCommand:
    """Parsed /set_plan_rearm command fields."""

    plan_id: int
    rearm_margin: float


@dataclass(frozen=True, slots=True)
class MarkAddedCommand:
    """Parsed explicit user statement that configured tiers were purchased."""

    plan_id: int
    tier_keys: tuple[str, ...]
    action_date: date | None = None


def _parse_quoted_args(
    args: Sequence[str],
    *,
    expected: Collection[int],
    usage: str,
) -> tuple[str, ...]:
    """Recover shell-style quoting from Telegram's whitespace-split args."""

    raw = tuple(args)
    has_quoted_arg = any(
        value.startswith(quote)
        and (
            (len(value) > 1 and value.endswith(quote))
            or any(later.endswith(quote) for later in raw[index + 1 :])
        )
        for index, value in enumerate(raw)
        for quote in ('"', "'")
    )
    if len(raw) in expected and not has_quoted_arg:
        return raw
    if not has_quoted_arg:
        raise CommandParseError(usage)
    try:
        words = tuple(shlex.split(" ".join(raw)))
    except ValueError as exc:
        raise CommandParseError(f"{usage}\n{exc}") from exc
    if len(words) not in expected:
        raise CommandParseError(usage)
    return words


def parse_add_drawdown_args(args: Sequence[str]) -> DrawdownCommand:
    """Parse /add_drawdown arguments into a typed command object."""

    raw_asset_type, symbol, name, raw_lookback_days, raw_thresholds = (
        _parse_quoted_args(args, expected={5}, usage=ADD_DRAWDOWN_USAGE)
    )
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

    raw_asset_type, symbol, name, raw_cost, raw_thresholds = _parse_quoted_args(
        args,
        expected={5},
        usage=ADD_PROFIT_USAGE,
    )
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
        try:
            validate_position_profit_notification_size(
                symbol=symbol,
                name=name,
                thresholds=thresholds,
            )
        except ValueError as exc:
            raise CommandParseError(str(exc)) from exc
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

    if len(args) not in {2, 3}:
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
    action_date = None
    if len(args) == 3:
        try:
            action_date = date.fromisoformat(args[2])
        except ValueError as exc:
            raise CommandParseError("action_date must use YYYY-MM-DD") from exc
    return MarkAddedCommand(plan_id, tier_keys, action_date)


def parse_add_drawdown_plan_args(args: Sequence[str]) -> DrawdownPlanCommand:
    """Parse a Reference ETF / feeder-fund drawdown plan."""

    try:
        words = shlex.split(" ".join(args))
    except ValueError as exc:
        raise CommandParseError(f"{ADD_DRAWDOWN_PLAN_USAGE}\n{exc}") from exc
    if len(words) not in {4, 5, 6}:
        raise CommandParseError(ADD_DRAWDOWN_PLAN_USAGE)

    reference_symbol = _parse_fund_symbol(words[0])
    investment_fund_symbol = _parse_fund_symbol(words[1])
    name = words[2].strip()
    if not name:
        raise CommandParseError("name must not be empty")
    lookback_days = 365
    rearm_margin = DEFAULT_REARM_MARGIN
    seen_options: set[str] = set()
    for option in words[4:]:
        if option.startswith("lookback:"):
            option_name = "lookback"
            if option_name in seen_options:
                raise CommandParseError("duplicate lookback option")
            seen_options.add(option_name)
            try:
                lookback_days = int(option.removeprefix("lookback:"))
            except ValueError as exc:
                raise CommandParseError("lookback must be a positive integer") from exc
            if lookback_days <= 0:
                raise CommandParseError("lookback must be a positive integer")
        elif option.startswith("rearm:"):
            option_name = "rearm"
            if option_name in seen_options:
                raise CommandParseError("duplicate rearm option")
            seen_options.add(option_name)
            rearm_margin = parse_rearm_percent(
                option.removeprefix("rearm:"),
            )
        else:
            raise CommandParseError(
                "unknown option; only trailing lookback:<calendar_days> or "
                "rearm:<percent> options are allowed"
            )

    tiers = _parse_drawdown_plan_tiers(words[3])
    params: dict[str, object] = {
        "investment_fund_symbol": investment_fund_symbol,
        "lookback_days": lookback_days,
        "rearm_margin": rearm_margin,
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


def parse_rearm_percent(raw_percent: str) -> float:
    """Parse a user-facing rearm percentage into a decimal fraction."""

    raw_value = raw_percent.strip()
    if raw_value.endswith("%"):
        raw_value = raw_value[:-1]
    try:
        percent = float(raw_value)
    except ValueError as exc:
        raise CommandParseError(
            "rearm percent must be finite and between 0 and 100"
        ) from exc
    if not math.isfinite(percent) or percent <= 0 or percent >= 100:
        raise CommandParseError(
            "rearm percent must be greater than 0 and less than 100"
        )
    return percent / 100


def parse_set_plan_rearm_args(args: Sequence[str]) -> SetPlanRearmCommand:
    """Parse /set_plan_rearm arguments."""

    if len(args) != 2:
        raise CommandParseError(SET_PLAN_REARM_USAGE)
    try:
        plan_id = int(args[0])
    except ValueError as exc:
        raise CommandParseError("plan_id must be a positive integer") from exc
    if plan_id <= 0:
        raise CommandParseError("plan_id must be a positive integer")
    return SetPlanRearmCommand(plan_id, parse_rearm_percent(args[1]))


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

    words = _parse_quoted_args(args, expected={3, 5, 6}, usage=ADD_DCA_USAGE)
    if len(words) == 3:
        raw_name, raw_weekday, raw_amount = words
        fund_symbol = None
        fee_mode = None
        fee_value = None
        holiday_policy = None
    else:
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
