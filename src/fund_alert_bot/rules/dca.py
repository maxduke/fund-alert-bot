"""DCA reminder rule helpers."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, tzinfo
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

from fund_alert_bot.rules._params import (
    read_params,
    read_required_param,
    read_required_rule_value,
    read_rule_value,
)

_read_params = partial(read_params, subject="rule")
_read_required_param = partial(read_required_param, subject="DCA rule")
_read_required_rule_value = partial(read_required_rule_value, subject="DCA rule")

AlertChecker = Callable[[str], bool]

WEEKDAY_CODES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

_CHINESE_WEEKDAYS = {
    "周一": "MON",
    "周二": "TUE",
    "周三": "WED",
    "周四": "THU",
    "周五": "FRI",
    "周六": "SAT",
    "周日": "SUN",
}
_ENGLISH_WEEKDAYS = {
    "monday": "MON",
    "tuesday": "TUE",
    "wednesday": "WED",
    "thursday": "THU",
    "friday": "FRI",
    "saturday": "SAT",
    "sunday": "SUN",
}


def normalize_weekday(raw_value: str) -> str:
    """Normalize supported weekday names to MON/TUE/WED/THU/FRI/SAT/SUN."""

    value = raw_value.strip()
    if value in _CHINESE_WEEKDAYS:
        return _CHINESE_WEEKDAYS[value]

    upper_value = value.upper()
    if upper_value in WEEKDAY_CODES:
        return upper_value

    lowered_value = value.lower()
    if lowered_value in _ENGLISH_WEEKDAYS:
        return _ENGLISH_WEEKDAYS[lowered_value]

    raise ValueError(
        "weekday must be one of 周一, 周二, 周三, 周四, 周五, 周六, 周日, "
        "or Monday through Sunday"
    )


def build_dca_reminder_alert(
    rule: Any,
    today: date,
    existing_alert_checker: AlertChecker,
    *,
    occurrence_status: str | None = None,
    effective_date: str | None = None,
    occurrence_amount: int | float | None = None,
) -> dict[str, object] | None:
    """Build a DCA reminder alert when the rule is due today."""

    params = _read_params(rule)
    weekday = normalize_weekday(str(_read_required_param(params, "weekday")))
    if weekday != weekday_for_date(today):
        return None

    rule_id = int(_read_required_rule_value(rule, "id"))
    alert_key = build_dca_alert_key(rule_id=rule_id, due_date=today)
    if existing_alert_checker(alert_key):
        return None

    amount = _read_amount(params)
    name = str(read_rule_value(rule, "name", ""))
    due_date = today.isoformat()
    enhanced = str(read_rule_value(rule, "asset_type", "")) == "cn_open_fund"
    if enhanced:
        if occurrence_amount is not None:
            amount = _read_amount({"amount": occurrence_amount})
        fund_symbol = str(read_rule_value(rule, "symbol", ""))
        holiday_policy = str(params.get("holiday_policy", "next"))
        status_line = (
            "Holiday policy skipped this occurrence; no position estimate will apply."
            if occurrence_status == "skipped"
            else (
                f"Estimated subscription NAV date: {effective_date}."
                if effective_date is not None
                else "Waiting for the next confirmed open day before estimating units."
            )
        )
        message = "\n".join(
            (
                "💰 Fixed DCA reminder",
                "",
                f"• Fund: {fund_symbol} / {name}",
                f"• Scheduled date: {due_date}",
                f"• Gross amount: {format_dca_amount(amount)} RMB",
                f"• Holiday policy: {holiday_policy}",
                f"• {status_line}",
                "",
                "The bot assumes the configured deduction executes; "
                "it does not verify it.",
                (
                    f"If deduction failed, use /dca_skip {rule_id} {due_date}."
                    if occurrence_status != "skipped"
                    else "No action is required for this configured holiday skip."
                ),
                "Remember to run /sync_position after any visible platform mismatch.",
                "Reminder only. No trade has been placed.",
            )
        )
    else:
        message = "\n".join(
            (
                "💰 DCA reminder",
                "",
                f"• 标的：{name}",
                f"• 日期：{due_date}",
                f"• 计划金额：{format_dca_amount(amount)} 元",
                "",
                "提醒：这是纪律提醒，不会自动交易。",
            )
        )
    payload = {
        "rule_id": rule_id,
        "name": name,
        "weekday": weekday,
        "amount": amount,
        "due_date": due_date,
    }
    if enhanced:
        payload.update(
            {
                "fund_symbol": fund_symbol,
                "holiday_policy": holiday_policy,
                "occurrence_status": occurrence_status,
                "effective_date": effective_date,
            }
        )
    return {
        "alert_key": alert_key,
        "title": "DCA reminder",
        "message": message,
        "payload": payload,
    }


def build_dca_alert_key(*, rule_id: int, due_date: date) -> str:
    """Build the once-per-day DCA reminder alert key."""

    return f"dca:{rule_id}:{due_date.isoformat()}"


def weekday_for_date(value: date) -> str:
    """Return the normalized weekday code for a date."""

    return WEEKDAY_CODES[value.weekday()]


def rule_creation_date(rule: Any, timezone: str | tzinfo) -> date:
    """Interpret legacy naive SQLite timestamps as UTC, then use the rule timezone."""

    created_at = datetime.fromisoformat(
        str(_read_required_rule_value(rule, "created_at"))
    )
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    zone = ZoneInfo(timezone) if isinstance(timezone, str) else timezone
    return created_at.astimezone(zone).date()


def _read_amount(params: Mapping[str, Any]) -> int | float:
    raw_amount = _read_required_param(params, "amount")
    if isinstance(raw_amount, bool):
        raise ValueError("DCA amount must be a positive number.")

    try:
        amount = float(raw_amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("DCA amount must be a positive number.") from exc

    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("DCA amount must be a positive number.")

    if amount.is_integer():
        return int(amount)
    return amount


def format_dca_amount(amount: int | float) -> str:
    """Format a configured DCA amount without hiding valid precision."""

    if isinstance(amount, int):
        return str(amount)
    return f"{amount:.12g}"
