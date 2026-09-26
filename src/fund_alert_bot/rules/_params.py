"""Parsing helpers shared by the drawdown, DCA and profit alert rules."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

# Absorbs float noise so a value that lands exactly on a threshold still meets it.
THRESHOLD_TOLERANCE = 1e-12


def meets_threshold(value: float, threshold: float) -> bool:
    return value + THRESHOLD_TOLERANCE >= threshold


def read_rule_value(rule: Any, key: str, default: Any) -> Any:
    """Read a field from a mapping, sqlite3.Row, or attribute-style rule."""

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


def read_params(rule: Any, *, subject: str) -> dict[str, Any]:
    """Return rule params from ``params`` or ``params_json`` as a dict."""

    params = read_rule_value(rule, "params", None)
    if params is None:
        params = read_rule_value(rule, "params_json", None)
    if params is None:
        return {}
    if isinstance(params, str):
        loaded = json.loads(params)
        if not isinstance(loaded, dict):
            raise ValueError(f"{subject} params_json must contain a JSON object.")
        return loaded
    if isinstance(params, Mapping):
        return dict(params)
    raise ValueError(f"{subject} params must be a mapping or JSON object string.")


def read_required_param(params: Mapping[str, Any], key: str, *, subject: str) -> Any:
    if key not in params:
        raise ValueError(f"{subject} missing required param: {key}")
    return params[key]


def read_required_rule_value(rule: Any, key: str, *, subject: str) -> Any:
    value = read_rule_value(rule, key, None)
    if value is None:
        raise ValueError(f"{subject} missing required field: {key}")
    return value
