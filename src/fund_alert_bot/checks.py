"""Shared alert check evaluation logic."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from fund_alert_bot.db import (
    alert_exists,
    list_enabled_rules,
    reserve_alert_event,
)
from fund_alert_bot.market_data import (
    AssetType,
    EmptyMarketDataError,
    Instrument,
    MarketDataProvider,
)
from fund_alert_bot.rules.dca import build_dca_reminder_alert
from fund_alert_bot.rules.drawdown import (
    build_drawdown_alerts,
    calculate_drawdown_from_high,
)
from fund_alert_bot.rules.profit import (
    LatestDataUnavailableError,
    build_profit_alerts,
    latest_unavailable_message,
)

DCA_RULE_TYPE = "dca_reminder"
DRAW_DOWN_RULE_TYPE = "drawdown_from_high"
PROFIT_RULE_TYPE = "profit_reminder"


@dataclass(frozen=True, slots=True)
class MarketDataCacheKey:
    """Market data identity shared by rules for the same instrument code."""

    symbol: str
    asset_type: AssetType


@dataclass(frozen=True, slots=True)
class DrawdownRuleContext:
    """Parsed drawdown rule fields used during one evaluator run."""

    row: Any
    params: dict[str, Any]
    lookback_days: int
    start_date: date
    instrument: Instrument
    cache_key: MarketDataCacheKey


@dataclass(frozen=True, slots=True)
class AlertNotification:
    """Alert text ready to send after the event has been reserved."""

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
class DrawdownRuleStatus:
    """Current drawdown snapshot for a checked rule."""

    rule_id: int
    symbol: str
    name: str
    latest_date: str
    latest_price: float
    peak_date: str
    peak_price: float
    drawdown: float


@dataclass(frozen=True, slots=True)
class DrawdownCheckResult:
    """Summary of one drawdown check run."""

    checked_rules: int
    notifications: list[AlertNotification]
    skipped_duplicates: int
    no_data_skips: list[RuleNoDataSkip]
    errors: list[RuleCheckError]
    statuses: list[DrawdownRuleStatus]


@dataclass(frozen=True, slots=True)
class DcaCheckResult:
    """Summary of one DCA reminder check run."""

    checked_rules: int
    notifications: list[AlertNotification]
    skipped_duplicates: int
    errors: list[RuleCheckError]


@dataclass(frozen=True, slots=True)
class ProfitCheckResult:
    """Summary of one profit reminder check run."""

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
    include_latest: bool = False,
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
    statuses: list[DrawdownRuleStatus] = []
    contexts: list[DrawdownRuleContext] = []
    market_data_cache = DrawdownMarketDataCache(
        market_data_provider=market_data_provider,
        end_date=end_date,
        include_latest=include_latest,
    )

    for row in rules:
        try:
            params = _load_params(row["params_json"])
            lookback_days = int(params["lookback_days"])
            start_date = end_date - timedelta(days=lookback_days)
            asset_type = AssetType(row["asset_type"])
            instrument = Instrument(
                symbol=row["symbol"],
                name=row["name"],
                asset_type=asset_type,
            )
            cache_key = MarketDataCacheKey(
                symbol=str(row["symbol"]),
                asset_type=asset_type,
            )
            context = DrawdownRuleContext(
                row=row,
                params=params,
                lookback_days=lookback_days,
                start_date=start_date,
                instrument=instrument,
                cache_key=cache_key,
            )
            contexts.append(context)
            market_data_cache.register_context(context)
        except Exception as exc:  # noqa: BLE001
            errors.append(
                RuleCheckError(
                    rule_id=int(row["id"]),
                    symbol=str(row["symbol"]),
                    message=str(exc),
                )
            )

    for context in contexts:
        row = context.row
        try:
            history = market_data_cache.history_for(context)
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

            current = calculate_drawdown_from_high(
                history,
                lookback_days=context.lookback_days,
                price_field=str(context.params.get("price_field", "close")),
            )
            statuses.append(
                DrawdownRuleStatus(
                    rule_id=int(row["id"]),
                    symbol=str(row["symbol"]),
                    name=str(row["name"]),
                    latest_date=str(current["latest_date"]),
                    latest_price=float(current["latest_price"]),
                    peak_date=str(current["peak_date"]),
                    peak_price=float(current["peak_price"]),
                    drawdown=float(current["drawdown"]),
                )
            )

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
                event_id = reserve_alert_event(
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
        statuses=statuses,
    )


def evaluate_profit_rules(
    connection: Any,
    market_data_provider: MarketDataProvider,
) -> ProfitCheckResult:
    """Evaluate enabled profit reminder rules and store new alert events."""

    rules = [
        row for row in list_enabled_rules(connection) if row["type"] == PROFIT_RULE_TYPE
    ]

    notifications: list[AlertNotification] = []
    errors: list[RuleCheckError] = []
    no_data_skips: list[RuleNoDataSkip] = []
    skipped_duplicates = 0
    latest_cache: dict[MarketDataCacheKey, dict[str, object] | None] = {}

    for row in rules:
        try:
            asset_type = AssetType(row["asset_type"])
            instrument = Instrument(
                symbol=row["symbol"],
                name=row["name"],
                asset_type=asset_type,
            )
            cache_key = MarketDataCacheKey(
                symbol=str(row["symbol"]),
                asset_type=asset_type,
            )
            if cache_key not in latest_cache:
                latest_cache[cache_key] = market_data_provider.get_latest(instrument)
            latest = latest_cache[cache_key]
            if latest is None:
                no_data_skips.append(
                    RuleNoDataSkip(
                        rule_id=int(row["id"]),
                        symbol=str(row["symbol"]),
                        message=latest_unavailable_message(
                            symbol=str(row["symbol"]),
                            asset_type=str(row["asset_type"]),
                        ),
                    )
                )
                continue

            alerts = build_profit_alerts(
                row,
                latest,
                lambda alert_key: alert_exists(connection, alert_key),
            )
        except LatestDataUnavailableError as exc:
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
                event_id = reserve_alert_event(
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

    return ProfitCheckResult(
        checked_rules=len(rules),
        notifications=notifications,
        skipped_duplicates=skipped_duplicates,
        no_data_skips=no_data_skips,
        errors=errors,
    )


def evaluate_dca_rules(
    connection: Any,
    *,
    today: date | None = None,
) -> DcaCheckResult:
    """Evaluate enabled DCA reminder rules and store new alert events."""

    check_date = today or date.today()
    rules = [
        row for row in list_enabled_rules(connection) if row["type"] == DCA_RULE_TYPE
    ]

    notifications: list[AlertNotification] = []
    errors: list[RuleCheckError] = []
    skipped_duplicates = 0

    for row in rules:
        try:
            alert = build_dca_reminder_alert(
                row,
                check_date,
                lambda alert_key: alert_exists(connection, alert_key),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                RuleCheckError(
                    rule_id=int(row["id"]),
                    symbol=str(row["symbol"]),
                    message=str(exc),
                )
            )
            continue

        if alert is None:
            continue

        try:
            event_id = reserve_alert_event(
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

    return DcaCheckResult(
        checked_rules=len(rules),
        notifications=notifications,
        skipped_duplicates=skipped_duplicates,
        errors=errors,
    )


class DrawdownMarketDataCache:
    """Per-run market data cache for drawdown rule evaluation."""

    def __init__(
        self,
        *,
        market_data_provider: MarketDataProvider,
        end_date: date,
        include_latest: bool,
    ) -> None:
        self._market_data_provider = market_data_provider
        self._end_date = end_date
        self._include_latest = include_latest
        self._earliest_start_by_instrument: dict[MarketDataCacheKey, date] = {}
        self._history_cache: dict[MarketDataCacheKey, pd.DataFrame] = {}
        self._history_errors: dict[MarketDataCacheKey, EmptyMarketDataError] = {}
        self._latest_cache: dict[MarketDataCacheKey, dict[str, object] | None] = {}
        self._combined_history_cache: dict[MarketDataCacheKey, pd.DataFrame] = {}

    def register_context(self, context: DrawdownRuleContext) -> None:
        """Record the widest required range for one instrument."""

        earliest_start = self._earliest_start_by_instrument.get(context.cache_key)
        if earliest_start is None or context.start_date < earliest_start:
            self._earliest_start_by_instrument[context.cache_key] = context.start_date

    def history_for(self, context: DrawdownRuleContext) -> pd.DataFrame:
        """Return cached history, optionally merged with cached latest data."""

        history = self._history_for(context)
        if not self._include_latest:
            return history

        if context.cache_key in self._combined_history_cache:
            return self._combined_history_cache[context.cache_key]

        if context.cache_key not in self._latest_cache:
            self._latest_cache[context.cache_key] = (
                self._market_data_provider.get_latest(context.instrument)
            )
        self._combined_history_cache[context.cache_key] = _append_latest_row(
            history,
            self._latest_cache[context.cache_key],
        )
        return self._combined_history_cache[context.cache_key]

    def _history_for(self, context: DrawdownRuleContext) -> pd.DataFrame:
        if context.cache_key in self._history_cache:
            return self._history_cache[context.cache_key]
        if context.cache_key in self._history_errors:
            raise self._history_errors[context.cache_key]

        start_date = self._earliest_start_by_instrument[context.cache_key]
        try:
            self._history_cache[context.cache_key] = (
                self._market_data_provider.get_history(
                    context.instrument,
                    start_date,
                    self._end_date,
                )
            )
        except EmptyMarketDataError as exc:
            self._history_errors[context.cache_key] = exc
            raise
        return self._history_cache[context.cache_key]


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


def _append_latest_row(
    history: pd.DataFrame,
    latest: dict[str, object] | None,
) -> pd.DataFrame:
    if latest is None:
        return history
    if "date" not in latest or "close" not in latest:
        return history

    latest_date = pd.to_datetime(latest["date"], errors="coerce")
    if pd.isna(latest_date):
        return history

    latest_row = {column: latest.get(column) for column in history.columns}
    latest_row["date"] = latest_date.normalize()
    frame = pd.concat([history, pd.DataFrame([latest_row])], ignore_index=True)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    return (
        frame.sort_values("date", ascending=True)
        .drop_duplicates(subset=["date"], keep="last")
        .reset_index(drop=True)
    )
