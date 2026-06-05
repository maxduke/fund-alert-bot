"""Shared alert check evaluation logic."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from fund_alert_bot.db import (
    add_alert_event,
    alert_exists,
    list_enabled_rules,
)
from fund_alert_bot.market_data import (
    AssetType,
    EmptyMarketDataError,
    Instrument,
    MarketDataProvider,
)
from fund_alert_bot.rules.drawdown import build_drawdown_alerts

DRAW_DOWN_RULE_TYPE = "drawdown_from_high"


@dataclass(frozen=True, slots=True)
class AlertNotification:
    """Alert text ready to send after the event has been stored."""

    event_id: int
    title: str
    text: str


@dataclass(frozen=True, slots=True)
class RuleNoDataSkip:
    """A rule skipped because the provider has no current market data."""

    rule_id: int
    symbol: str
    message: str


@dataclass(frozen=True, slots=True)
class RuleCheckError:
    """A per-rule check error."""

    rule_id: int
    symbol: str
    message: str


@dataclass(frozen=True, slots=True)
class DrawdownCheckResult:
    """Summary of one drawdown check run."""

    checked_rules: int
    notifications: list[AlertNotification]
    skipped_duplicates: int
    no_data_skips: list[RuleNoDataSkip]
    errors: list[RuleCheckError]


def evaluate_drawdown_rules(
    connection: Any,
    market_data_provider: MarketDataProvider,
    *,
    today: date | None = None,
    require_new_data_date: date | None = None,
) -> DrawdownCheckResult:
    """Evaluate all enabled drawdown rules and store new alert events."""

    end_date = today or require_new_data_date or date.today()
    rules = [
        row
        for row in list_enabled_rules(connection)
        if row["type"] == DRAW_DOWN_RULE_TYPE
    ]

    notifications: list[AlertNotification] = []
    errors: list[RuleCheckError] = []
    no_data_skips: list[RuleNoDataSkip] = []
    skipped_duplicates = 0

    for row in rules:
        try:
            params = _load_params(row["params_json"])
            lookback_days = int(params["lookback_days"])
            start_date = end_date - timedelta(days=lookback_days)
            instrument = Instrument(
                symbol=row["symbol"],
                name=row["name"],
                asset_type=AssetType(row["asset_type"]),
            )
            history = market_data_provider.get_history(
                instrument,
                start_date,
                end_date,
            )
            if require_new_data_date is not None:
                latest_data_date = _latest_history_date(history)
                if latest_data_date != require_new_data_date:
                    no_data_skips.append(
                        RuleNoDataSkip(
                            rule_id=int(row["id"]),
                            symbol=str(row["symbol"]),
                            message=_format_no_data_message(
                                expected_date=require_new_data_date,
                                latest_data_date=latest_data_date,
                            ),
                        )
                    )
                    continue

            alerts = build_drawdown_alerts(
                row,
                history,
                lambda alert_key: alert_exists(connection, alert_key),
            )
        except EmptyMarketDataError as exc:
            no_data_skips.append(
                RuleNoDataSkip(
                    rule_id=int(row["id"]),
                    symbol=str(row["symbol"]),
                    message=str(exc),
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(
                RuleCheckError(
                    rule_id=int(row["id"]),
                    symbol=str(row["symbol"]),
                    message=str(exc),
                )
            )
            continue

        for alert in alerts:
            try:
                event_id = add_alert_event(
                    connection,
                    rule_id=int(row["id"]),
                    alert_key=str(alert["alert_key"]),
                    title=str(alert["title"]),
                    message=str(alert["message"]),
                    payload=alert.get("payload"),
                )
            except sqlite3.IntegrityError:
                skipped_duplicates += 1
                continue

            notifications.append(
                AlertNotification(
                    event_id=event_id,
                    title=str(alert["title"]),
                    text=str(alert["message"]),
                )
            )

    return DrawdownCheckResult(
        checked_rules=len(rules),
        notifications=notifications,
        skipped_duplicates=skipped_duplicates,
        no_data_skips=no_data_skips,
        errors=errors,
    )


def _load_params(params_json: str) -> dict[str, Any]:
    params = json.loads(params_json)
    if not isinstance(params, dict):
        raise ValueError("params_json must contain a JSON object")
    return params


def _latest_history_date(history: pd.DataFrame) -> date | None:
    if history.empty or "date" not in history.columns:
        return None

    dates = pd.to_datetime(history["date"], errors="coerce").dropna()
    if dates.empty:
        return None
    return dates.max().date()


def _format_no_data_message(
    *,
    expected_date: date,
    latest_data_date: date | None,
) -> str:
    if latest_data_date is None:
        return f"No market data available for {expected_date.isoformat()}."
    return (
        f"No market data available for {expected_date.isoformat()}; "
        f"latest data date is {latest_data_date.isoformat()}."
    )
