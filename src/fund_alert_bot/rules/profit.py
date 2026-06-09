"""Profit-taking reminder rule helpers."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

from fund_alert_bot.market_data import AssetType

AlertChecker = Callable[[str], bool]

_THRESHOLD_TOLERANCE = 1e-12


class LatestDataUnavailableError(ValueError):
    """Raised when latest price data cannot be used for a profit reminder."""


def calculate_profit_rate(*, current_price: float, cost: float) -> float:
    """Calculate profit rate from current price and cost basis."""

    current_price = _to_positive_float(current_price, "current_price")
    cost = _to_positive_float(cost, "cost")
    return current_price / cost - 1


def build_profit_alerts(
    rule: Any,
    latest: Mapping[str, object],
    existing_alert_checker: AlertChecker,
) -> list[dict[str, object]]:
    """Build profit reminder alert records for crossed thresholds."""

    params = _read_params(rule)
    cost = _read_cost(params)
    thresholds = _read_thresholds(params)
    symbol = str(_read_required_rule_value(rule, "symbol"))
    name = str(_read_rule_value(rule, "name", ""))
    asset_type = str(_read_rule_value(rule, "asset_type", ""))
    current_price = _read_latest_close(
        latest,
        symbol=symbol,
        asset_type=asset_type,
    )
    profit_rate = calculate_profit_rate(current_price=current_price, cost=cost)

    alerts: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for threshold in thresholds:
        if not _meets_threshold(profit_rate, threshold):
            continue

        alert_key = build_profit_alert_key(
            symbol=symbol,
            cost=cost,
            threshold=threshold,
        )
        if alert_key in seen_keys or existing_alert_checker(alert_key):
            continue

        seen_keys.add(alert_key)
        alerts.append(
            {
                "alert_key": alert_key,
                "title": "💵 Profit-taking reminder",
                "message": _build_message(
                    symbol=symbol,
                    name=name,
                    asset_type=asset_type,
                    cost=cost,
                    current_price=current_price,
                    profit_rate=profit_rate,
                    threshold=threshold,
                ),
                "payload": {
                    "symbol": symbol,
                    "name": name,
                    "asset_type": asset_type,
                    "cost": cost,
                    "latest_price": current_price,
                    "latest_date": _format_optional_date(latest.get("date")),
                    "profit_rate": profit_rate,
                    "threshold": threshold,
                    "source": _read_optional_latest_value(latest, "source"),
                },
            }
        )

    return alerts


def build_profit_alert_key(*, symbol: str, cost: float, threshold: float) -> str:
    """Build the once-per-cost-basis profit reminder alert key."""

    return (
        f"{symbol}:profit:cost:{_format_number(cost)}:"
        f"threshold:{_format_number(threshold)}"
    )


def latest_unavailable_message(*, symbol: str, asset_type: str) -> str:
    """Build a clear latest-price unavailable message for a rule."""

    return f"Latest {_price_name(asset_type)} is unavailable for {symbol}."


def _read_params(rule: Any) -> dict[str, Any]:
    params = _read_rule_value(rule, "params", None)
    if params is None:
        params = _read_rule_value(rule, "params_json", None)
    if params is None:
        return {}
    if isinstance(params, str):
        loaded = json.loads(params)
        if not isinstance(loaded, dict):
            raise ValueError("profit rule params_json must contain a JSON object.")
        return loaded
    if isinstance(params, Mapping):
        return dict(params)
    raise ValueError("profit rule params must be a mapping or JSON object string.")


def _read_cost(params: Mapping[str, Any]) -> float:
    raw_cost = _read_required_param(params, "cost")
    return _to_positive_float(raw_cost, "cost")


def _read_thresholds(params: Mapping[str, Any]) -> list[float]:
    raw_thresholds = _read_required_param(params, "thresholds")
    if isinstance(raw_thresholds, str) or not isinstance(raw_thresholds, Sequence):
        raise ValueError("profit thresholds must be a sequence of numbers.")

    thresholds = [float(threshold) for threshold in raw_thresholds]
    if not thresholds:
        raise ValueError("profit thresholds must not be empty.")
    if any(threshold <= 0 or threshold >= 1 for threshold in thresholds):
        raise ValueError("profit thresholds must be between 0 and 1.")
    return thresholds


def _read_required_param(params: Mapping[str, Any], key: str) -> Any:
    if key not in params:
        raise ValueError(f"profit rule missing required param: {key}")
    return params[key]


def _read_required_rule_value(rule: Any, key: str) -> Any:
    value = _read_rule_value(rule, key, None)
    if value is None:
        raise ValueError(f"profit rule missing required field: {key}")
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


def _read_latest_close(
    latest: Mapping[str, object],
    *,
    symbol: str,
    asset_type: str,
) -> float:
    raw_close = latest.get("close")
    if raw_close is None:
        raise LatestDataUnavailableError(
            latest_unavailable_message(symbol=symbol, asset_type=asset_type)
        )

    try:
        close = float(raw_close)
    except (TypeError, ValueError) as exc:
        raise LatestDataUnavailableError(
            latest_unavailable_message(symbol=symbol, asset_type=asset_type)
        ) from exc

    if not math.isfinite(close) or close <= 0:
        raise LatestDataUnavailableError(
            latest_unavailable_message(symbol=symbol, asset_type=asset_type)
        )
    return close


def _read_optional_latest_value(
    latest: Mapping[str, object],
    key: str,
) -> object | None:
    value = latest.get(key)
    if value is None or pd.isna(value):
        return None
    return value


def _build_message(
    *,
    symbol: str,
    name: str,
    asset_type: str,
    cost: float,
    current_price: float,
    profit_rate: float,
    threshold: float,
) -> str:
    return "\n".join(
        (
            "💵 Profit-taking reminder",
            "",
            f"• Symbol: {symbol}",
            f"• Name: {name}",
            f"• Asset type: {asset_type}",
            f"• Cost: {_format_number(cost)}",
            f"• {_price_label(asset_type)}: {_format_number(current_price)}",
            f"• Profit rate: {profit_rate:.1%}",
            f"• Triggered threshold: {threshold:.1%}",
            "",
            "Reminder: this is not automatic trading and no orders will be placed.",
        )
    )


def _meets_threshold(profit_rate: float, threshold: float) -> bool:
    return profit_rate + _THRESHOLD_TOLERANCE >= threshold


def _to_positive_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive number.")

    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive number.") from exc

    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return parsed


def _format_optional_date(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date().isoformat()


def _price_label(asset_type: str) -> str:
    if _resolve_asset_type(asset_type) is AssetType.CN_OPEN_FUND:
        return "Latest NAV"
    return "Latest price"


def _price_name(asset_type: str) -> str:
    if _resolve_asset_type(asset_type) is AssetType.CN_OPEN_FUND:
        return "unit NAV"
    return "price"


def _resolve_asset_type(asset_type: str) -> AssetType | None:
    try:
        return AssetType(asset_type)
    except ValueError:
        return None


def _format_number(value: float) -> str:
    return f"{value:.12g}"
