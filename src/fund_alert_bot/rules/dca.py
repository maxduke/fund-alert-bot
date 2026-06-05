"""DCA reminder rule helpers."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

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
    name = str(_read_rule_value(rule, "name", ""))
    due_date = today.isoformat()
    return {
        "alert_key": alert_key,
        "title": "DCA reminder",
        "message": (
            f"今天是 {name} 定投日，计划定投 {_format_amount(amount)} 元。\n"
            "提醒：这是纪律提醒，不会自动交易。"
        ),
        "payload": {
            "rule_id": rule_id,
            "name": name,
            "weekday": weekday,
            "amount": amount,
            "due_date": due_date,
        },
    }


def build_dca_alert_key(*, rule_id: int, due_date: date) -> str:
    """Build the once-per-day DCA reminder alert key."""

    return f"dca:{rule_id}:{due_date.isoformat()}"


def weekday_for_date(value: date) -> str:
    """Return the normalized weekday code for a date."""

    return WEEKDAY_CODES[value.weekday()]


def _read_params(rule: Any) -> dict[str, Any]:
    params = _read_rule_value(rule, "params", None)
    if params is None:
        params = _read_rule_value(rule, "params_json", None)
    if params is None:
        return {}
    if isinstance(params, str):
        loaded = json.loads(params)
        if not isinstance(loaded, dict):
            raise ValueError("rule params_json must contain a JSON object.")
        return loaded
    if isinstance(params, Mapping):
        return dict(params)
    raise ValueError("rule params must be a mapping or JSON object string.")


def _read_required_param(params: Mapping[str, Any], key: str) -> Any:
    if key not in params:
        raise ValueError(f"DCA rule missing required param: {key}")
    return params[key]


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


def _read_required_rule_value(rule: Any, key: str) -> Any:
    value = _read_rule_value(rule, key, None)
    if value is None:
        raise ValueError(f"DCA rule missing required field: {key}")
    return value


def _read_rule_value(rule: Any, key: str, default: Any) -> Any:
    if isinstance(rule, Mapping):
        return rule.get(key, default)

    keys = getattr(rule, "keys", None)
    if callable(keys) and key in keys():
        return rule[key]

    if hasattr(rule, key):
        return getattr(rule, key)

    try:
        return rule[key]
    except (KeyError, IndexError, TypeError):
        return default


def _format_amount(amount: int | float) -> str:
    if isinstance(amount, int):
        return str(amount)
    return f"{amount:.12g}"
