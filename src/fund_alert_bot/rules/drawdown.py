"""Drawdown-from-high alert rule."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pandas as pd

AlertChecker = Callable[[str], bool]

_THRESHOLD_TOLERANCE = 1e-12


def calculate_drawdown_from_high(
    df: pd.DataFrame,
    lookback_days: int,
    price_field: str = "close",
) -> dict[str, object]:
    """Calculate latest drawdown from the peak price in a calendar lookback."""

    if df.empty:
        raise ValueError("Market data is empty.")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive.")
    if price_field not in df.columns:
        raise ValueError(f"Market data is missing price field: {price_field}")
    if "date" not in df.columns:
        raise ValueError("Market data is missing date field.")

    frame = df.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise ValueError("Market data contains invalid dates.")

    frame[price_field] = pd.to_numeric(frame[price_field], errors="coerce")
    frame = frame.sort_values("date", ascending=True).reset_index(drop=True)

    latest_row = frame.iloc[-1]
    latest_price = _to_float(latest_row[price_field], "latest price")
    latest_date = latest_row["date"]
    window_start = latest_date - pd.Timedelta(days=lookback_days)
    window = frame.loc[frame["date"].between(window_start, latest_date)].copy()
    window = window.dropna(subset=[price_field])
    if window.empty:
        raise ValueError("Market data has no prices in the lookback window.")

    peak_index = window[price_field].idxmax()
    peak_row = window.loc[peak_index]
    peak_price = _to_float(peak_row[price_field], "peak price")
    if peak_price <= 0:
        raise ValueError("peak price must be positive.")

    drawdown = 1 - latest_price / peak_price
    return {
        "latest_date": _format_date(latest_date),
        "latest_price": latest_price,
        "peak_date": _format_date(peak_row["date"]),
        "peak_price": peak_price,
        "drawdown": drawdown,
        "source": _read_optional_row_value(latest_row, "source"),
    }


def build_drawdown_alerts(
    rule: Any,
    df: pd.DataFrame,
    existing_alert_checker: AlertChecker,
) -> list[dict[str, object]]:
    """Build drawdown alert records for crossed thresholds."""

    params = _read_params(rule)
    lookback_days = int(_read_required_param(params, "lookback_days"))
    thresholds = _read_thresholds(params)
    price_field = str(params.get("price_field", "close"))

    result = calculate_drawdown_from_high(
        df,
        lookback_days=lookback_days,
        price_field=price_field,
    )
    drawdown = float(result["drawdown"])
    symbol = str(_read_required_rule_value(rule, "symbol"))
    name = str(_read_rule_value(rule, "name", ""))
    asset_type = str(_read_rule_value(rule, "asset_type", ""))

    alerts: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for threshold in thresholds:
        if not _meets_threshold(drawdown, threshold):
            continue

        alert_key = _build_alert_key(
            symbol=symbol,
            lookback_days=lookback_days,
            peak_date=str(result["peak_date"]),
            threshold=threshold,
        )
        if alert_key in seen_keys or existing_alert_checker(alert_key):
            continue

        seen_keys.add(alert_key)
        alerts.append(
            {
                "alert_key": alert_key,
                "title": "Drawdown reminder",
                "message": (
                    f"{symbol} is down {drawdown:.1%} from its "
                    f"{lookback_days}-day high."
                ),
                "payload": {
                    "symbol": symbol,
                    "name": name,
                    "asset_type": asset_type,
                    "latest_date": result["latest_date"],
                    "latest_close": result["latest_price"],
                    "peak_date": result["peak_date"],
                    "peak_close": result["peak_price"],
                    "drawdown": drawdown,
                    "threshold": threshold,
                    "source": result["source"],
                },
            }
        )

    return alerts


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
        raise ValueError(f"drawdown rule missing required param: {key}")
    return params[key]


def _read_thresholds(params: Mapping[str, Any]) -> list[float]:
    raw_thresholds = _read_required_param(params, "thresholds")
    if isinstance(raw_thresholds, str) or not isinstance(raw_thresholds, Sequence):
        raise ValueError("thresholds must be a sequence of numbers.")

    thresholds = [float(threshold) for threshold in raw_thresholds]
    if not thresholds:
        raise ValueError("thresholds must not be empty.")
    if any(threshold <= 0 or threshold >= 1 for threshold in thresholds):
        raise ValueError("thresholds must be between 0 and 1.")
    return thresholds


def _read_required_rule_value(rule: Any, key: str) -> Any:
    value = _read_rule_value(rule, key, None)
    if value is None:
        raise ValueError(f"drawdown rule missing required field: {key}")
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


def _read_optional_row_value(row: pd.Series, key: str) -> object | None:
    if key not in row:
        return None
    value = row[key]
    if pd.isna(value):
        return None
    return value


def _build_alert_key(
    *,
    symbol: str,
    lookback_days: int,
    peak_date: str,
    threshold: float,
) -> str:
    return (
        f"{symbol}:drawdown:{lookback_days}:peak:{peak_date}:"
        f"threshold:{_format_threshold(threshold)}"
    )


def _meets_threshold(drawdown: float, threshold: float) -> bool:
    return drawdown + _THRESHOLD_TOLERANCE >= threshold


def _to_float(value: object, label: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite.")
    return parsed


def _format_date(value: object) -> str:
    return pd.Timestamp(value).date().isoformat()


def _format_threshold(threshold: float) -> str:
    return f"{threshold:.12g}"
