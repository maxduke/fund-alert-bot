"""Shared alert check evaluation logic."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from fund_alert_bot.db import (
    add_alert_event,
    add_drawdown_plan_pre_alert_event,
    alert_exists,
    apply_manual_add_estimate,
    apply_scheduled_dca_occurrence,
    create_scheduled_dca_occurrence,
    get_active_drawdown_cycle,
    get_active_position_cycle,
    get_cached_fund_nav,
    get_drawdown_tier_reminder_states,
    get_fund_settings,
    get_position_snapshot,
    has_position_profit_evaluation,
    list_drawdown_tier_records,
    list_enabled_rules,
    list_manual_add_actions,
    list_pending_manual_add_estimates,
    list_pending_scheduled_dca_occurrences,
    list_position_profit_threshold_keys,
    load_market_history,
    persist_drawdown_plan_evaluation,
    persist_position_profit_alert,
    record_position_profit_evaluation,
    reserve_alert_event,
    set_scheduled_dca_effective_date,
    skip_scheduled_dca_occurrence,
    upsert_fund_nav,
    upsert_market_history,
)
from fund_alert_bot.market_data import (
    AssetType,
    EmptyMarketDataError,
    FundNav,
    Instrument,
    MarketCalendar,
    MarketCalendarUnavailableError,
    MarketDataFetchError,
    MarketDataProvider,
    MarketDataProviderError,
    PriceBasis,
)
from fund_alert_bot.market_data.normalize import NORMALIZED_COLUMNS
from fund_alert_bot.rules.dca import (
    build_dca_reminder_alert,
    normalize_weekday,
    weekday_for_date,
)
from fund_alert_bot.rules.drawdown import (
    build_drawdown_alerts,
    calculate_drawdown_from_high,
)
from fund_alert_bot.rules.drawdown_plan import (
    ActiveDrawdownCycle,
    DrawdownPlanConfig,
    DrawdownPlanEvaluation,
    DrawdownTier,
    build_drawdown_plan_alert,
    build_drawdown_plan_pre_alert,
    evaluate_drawdown_plan,
    evaluate_drawdown_plan_realtime,
    format_plan_amount,
    format_plan_percent,
    parse_drawdown_plan_config,
    required_history_start,
    select_actionable_tiers,
    validate_drawdown_plan_notification_size,
)
from fund_alert_bot.rules.profit import (
    LatestDataUnavailableError,
    build_position_profit_alert,
    build_profit_alerts,
    latest_unavailable_message,
)

DCA_RULE_TYPE = "dca_reminder"
DRAW_DOWN_RULE_TYPE = "drawdown_from_high"
PROFIT_RULE_TYPE = "profit_reminder"
DRAW_DOWN_PLAN_RULE_TYPE = "drawdown_plan"
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketDataCacheKey:
    """Market data identity shared by rules for the same instrument code."""

    symbol: str
    asset_type: AssetType
    price_basis: PriceBasis = PriceBasis.UNADJUSTED


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
class DcaNotificationSummary:
    """Fields used to merge same-day fixed-DCA notification presentation."""

    due_date: str
    lines: tuple[str, ...]
    amount: float
    skipped: bool
    rebuilt_text: str | None = None


def build_dca_notification_summary(
    *,
    message: str,
    due_date: str,
    amount: float,
    skipped: bool,
    current_status: str | None = None,
    current_effective_date: str | None = None,
) -> DcaNotificationSummary:
    """Build the concise portion of a persisted fixed-DCA reminder."""

    lines = message.splitlines()
    status_line = lines[6]
    if current_status == "skipped" and "Holiday policy skipped" not in status_line:
        status_line = "• Deduction failed/not executed; this occurrence is skipped."
    elif current_status == "applied":
        status_line = "• This estimate was already applied."
    elif current_status == "reconciled_by_sync":
        status_line = "• This occurrence was already reconciled by Position Sync."
    elif current_status == "pending" and current_effective_date is not None:
        status_line = f"• Estimated subscription NAV date: {current_effective_date}."
    lines[6] = status_line
    resolved = current_status in {"skipped", "applied", "reconciled_by_sync"}
    action_line = None if skipped or resolved else lines[9]
    if resolved:
        lines.pop(9)
    return DcaNotificationSummary(
        due_date=due_date,
        lines=(lines[2], *lines[4:6], status_line)
        + ((action_line,) if action_line else ()),
        amount=amount,
        skipped=skipped,
        rebuilt_text="\n".join(lines) if current_status is not None else None,
    )


@dataclass(frozen=True, slots=True)
class AlertNotification:
    """Alert text ready to send after the event has been reserved."""

    event_id: int
    title: str
    text: str
    telegram_actions: tuple[tuple[tuple[str, str], ...], ...] = ()
    dca_summary: DcaNotificationSummary | None = None


@dataclass(frozen=True, slots=True)
class RuleNoDataSkip:
    """A rule skipped because the provider has no current market data."""

    rule_id: int
    symbol: str
    message: str
    data_date: date | None = None


@dataclass(frozen=True, slots=True)
class RuleCheckError:
    """A per-rule check error."""

    rule_id: int
    symbol: str
    message: str
    data_date: date | None = None


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
    data_date: date | None = None


@dataclass(frozen=True, slots=True)
class DrawdownPlanRuleResult:
    """Persisted result of one confirmed-history plan evaluation."""

    cycle_id: int
    evaluation: DrawdownPlanEvaluation
    notification: AlertNotification | None


@dataclass(frozen=True, slots=True)
class DrawdownPlanCheckResult:
    """Summary of confirmed-close evaluations for enabled plans."""

    checked_rules: int
    notifications: list[AlertNotification]
    no_data_skips: list[RuleNoDataSkip]
    errors: list[RuleCheckError]


@dataclass(frozen=True, slots=True)
class ManualAddSettlementResult:
    """Summary of one exact-date feeder-NAV processing run."""

    checked_estimates: int
    notifications: list[AlertNotification]
    no_data_skips: list[RuleNoDataSkip]
    errors: list[RuleCheckError]


def reserve_drawdown_plan_data_unavailable_notice(
    connection: Any,
    *,
    evaluation_date: date,
    result: Any,
    phase: str = "after_close",
) -> AlertNotification | None:
    """Reserve one phase-level notice for plans that could not be evaluated."""

    affected = [*result.no_data_skips, *result.errors]
    if not affected:
        return None
    if phase not in {"before_close", "after_close", "fund_nav"}:
        raise ValueError("Unsupported drawdown plan notice phase.")

    phase_label = {
        "before_close": "Before-close estimate",
        "after_close": "After-close confirmation",
        "fund_nav": "Feeder-fund NAV settlement",
    }[phase]
    alert_key = f"data_unavailable:{phase}:{evaluation_date.isoformat()}"
    notice_name = (
        "Feeder-fund data unavailable"
        if phase == "fund_nav"
        else "Drawdown plan data unavailable"
    )
    lines = [
        f"⚠️ {notice_name}",
        "",
        f"Data date: {evaluation_date.isoformat()}",
        f"{phase_label} could not evaluate:",
        *(
            f"• {evaluation_date.isoformat()} / {item.symbol}: {item.message}"
            for item in affected
        ),
        "",
        (
            "Pending position work was not applied and no Price-Gain decision was made."
            if phase == "fund_nav"
            else "No tier decision was made for these plans."
        ),
        "Please check your own platform.",
        "",
        "This is a reminder only. No trade has been placed.",
    ]
    message = "\n".join(lines)
    try:
        event_id = add_alert_event(
            connection,
            rule_id=affected[0].rule_id,
            alert_key=alert_key,
            title=notice_name,
            message=message,
            payload={
                "phase": phase,
                "data_date": evaluation_date.isoformat(),
                "affected_plans": [
                    {
                        "rule_id": item.rule_id,
                        "symbol": item.symbol,
                        "reason": item.message,
                        "data_date": evaluation_date.isoformat(),
                    }
                    for item in affected
                ],
            },
        )
    except sqlite3.IntegrityError:
        return None
    return AlertNotification(event_id=event_id, title=notice_name, text=message)


@dataclass(frozen=True, slots=True)
class DrawdownPlanStatus:
    """Read-only current state for one enabled drawdown plan."""

    rule_id: int
    reference_symbol: str
    name: str
    config: DrawdownPlanConfig
    evaluation: DrawdownPlanEvaluation
    recorded_tier_keys: frozenset[str]
    added_tier_keys: frozenset[str]
    readiness: str
    missing_setup: tuple[str, ...]
    position: Any | None
    fund_nav: FundNav | None
    position_sync_required_since: str | None


@dataclass(frozen=True, slots=True)
class DrawdownPlanStatusResult:
    """Read-only statuses plus per-plan failures."""

    checked_rules: int
    statuses: list[DrawdownPlanStatus]
    no_data_skips: list[RuleNoDataSkip]
    errors: list[RuleCheckError]


def evaluate_drawdown_plan_rule(
    connection: Any,
    rule: Any,
    history: pd.DataFrame,
    *,
    expected_date: date,
    same_day_actions: bool = True,
    initialize_only: bool = False,
) -> DrawdownPlanRuleResult:
    """Evaluate and atomically persist one drawdown plan."""

    if str(rule["type"]) != DRAW_DOWN_PLAN_RULE_TYPE:
        raise ValueError("rule type must be drawdown_plan.")
    rule_id = int(rule["id"])
    reference_symbol = str(rule["symbol"])
    name = str(rule["name"]).strip()
    if not name:
        raise ValueError("drawdown_plan name must not be empty.")
    config, active_cycle, recorded_tier_keys = _load_drawdown_plan_state(
        connection, rule
    )

    evaluation = evaluate_drawdown_plan(
        history,
        config,
        reference_symbol=reference_symbol,
        expected_date=expected_date,
        active_cycle=active_cycle,
        recorded_tier_keys=recorded_tier_keys,
    )
    cycle_changed = active_cycle is None or evaluation.cycle_changed
    added_tier_keys, skipped_tier_keys, snoozed_tier_keys = (
        (frozenset(), frozenset(), frozenset())
        if cycle_changed or active_cycle is None
        else _drawdown_reminder_key_sets(
            connection,
            active_cycle.cycle_id,
            market_date=evaluation.latest_date,
        )
    )
    actionable_tiers = (
        ()
        if initialize_only
        else select_actionable_tiers(
            config=config,
            current_drawdown=evaluation.drawdown,
            added_tier_keys=added_tier_keys,
            skipped_tier_keys=skipped_tier_keys,
            snoozed_tier_keys=snoozed_tier_keys,
        )
    )
    actionable_keys = {tier.key for tier in actionable_tiers}
    newly_crossed_tiers = tuple(
        tier for tier in evaluation.newly_crossed_tiers if tier.key in actionable_keys
    )
    newly_keys = {tier.key for tier in newly_crossed_tiers}
    pending_tiers = tuple(
        tier for tier in actionable_tiers if tier.key not in newly_keys
    )
    tiers_to_record = () if initialize_only else evaluation.newly_crossed_tiers
    resolved_alert: dict[str, object] | None = None

    def build_alert(cycle_id: int) -> dict[str, object] | None:
        nonlocal resolved_alert
        resolved_alert = build_drawdown_plan_alert(
            rule_id=rule_id,
            reference_symbol=reference_symbol,
            name=name,
            config=config,
            evaluation=evaluation,
            cycle_id=cycle_id,
            actionable_tiers=actionable_tiers,
            pending_tiers=pending_tiers,
        )
        if resolved_alert is not None and not same_day_actions:
            resolved_alert["message"] = format_delayed_drawdown_plan_message(
                str(resolved_alert["message"])
            )
        return resolved_alert

    cycle_id, event_id = persist_drawdown_plan_evaluation(
        connection,
        rule_id=rule_id,
        expected_active_cycle_id=(
            None if active_cycle is None else active_cycle.cycle_id
        ),
        expected_last_evaluated_date=(
            None
            if active_cycle is None
            else active_cycle.last_evaluated_date.isoformat()
        ),
        start_new_cycle=active_cycle is None or evaluation.cycle_changed,
        peak_date=evaluation.peak_date.isoformat(),
        peak_price=evaluation.peak_price,
        evaluation_date=evaluation.latest_date.isoformat(),
        tiers_to_record=tiers_to_record,
        alert_factory=(None if not actionable_tiers else build_alert),
    )
    LOGGER.info(
        "Drawdown plan evaluation rule_id=%s cycle_id=%s symbol=%s evaluation_date=%s "
        "latest_price=%s peak_price=%s drawdown=%s newly_crossed_tiers=%s "
        "actionable_tiers=%s sma=%s distance_to_sma=%s sma_slope=%s "
        "alert_reserved=%s",
        rule_id,
        cycle_id,
        reference_symbol,
        evaluation.latest_date,
        evaluation.latest_price,
        evaluation.peak_price,
        evaluation.drawdown,
        [tier.key for tier in tiers_to_record],
        [tier.key for tier in actionable_tiers],
        evaluation.sma,
        evaluation.distance_to_sma,
        evaluation.sma_slope,
        event_id is not None,
    )
    notification = None
    if event_id is not None and resolved_alert is not None:
        notification = AlertNotification(
            event_id=event_id,
            title=str(resolved_alert["title"]),
            text=str(resolved_alert["message"]),
            telegram_actions=(
                _drawdown_plan_action_rows(rule_id, event_id, actionable_tiers)
                if same_day_actions
                else ()
            ),
        )
    return DrawdownPlanRuleResult(
        cycle_id=cycle_id,
        evaluation=evaluation,
        notification=notification,
    )


def format_delayed_drawdown_plan_message(message: str) -> str:
    """Remove expired same-day actions from a delayed plan reminder."""

    alert_text, marker, _remainder = message.partition(
        "\nOnly after you actually subscribe, record it with:"
    )
    if not marker:
        return message
    return (
        f"{alert_text}\n"
        "Delayed confirmation for an earlier trading day; "
        "action buttons are unavailable.\n"
        "If you bought, wait for the fund platform to settle, then run "
        "/sync_position.\n"
        "This is a reminder only. No trade has been placed."
    )


def evaluate_drawdown_plan_rules(
    connection: Any,
    market_data_provider: MarketDataProvider,
    *,
    expected_date: date,
) -> DrawdownPlanCheckResult:
    """Evaluate and persist every enabled plan for one confirmed close date."""

    rules = [
        row
        for row in list_enabled_rules(connection)
        if row["type"] == DRAW_DOWN_PLAN_RULE_TYPE
    ]
    notifications: list[AlertNotification] = []
    no_data_skips: list[RuleNoDataSkip] = []
    errors: list[RuleCheckError] = []
    for rule in rules:
        try:
            active = get_active_drawdown_cycle(connection, int(rule["id"]))
            if active is not None and str(active["last_evaluated_date"]) >= (
                expected_date.isoformat()
            ):
                continue
            history = _fetch_drawdown_plan_history(
                connection,
                rule,
                market_data_provider,
                end_date=expected_date,
            )
            result = evaluate_drawdown_plan_rule(
                connection,
                rule,
                history,
                expected_date=expected_date,
            )
            if result.notification is not None:
                notifications.append(result.notification)
        except MarketDataProviderError as exc:
            no_data_skips.append(_plan_no_data_skip(rule, exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(_plan_check_error(rule, exc))
    return DrawdownPlanCheckResult(
        checked_rules=len(rules),
        notifications=notifications,
        no_data_skips=no_data_skips,
        errors=errors,
    )


def evaluate_drawdown_plan_prealerts(
    connection: Any,
    market_data_provider: MarketDataProvider,
    *,
    market_date: date,
    confirmed_date: date,
) -> DrawdownPlanCheckResult:
    """Evaluate provisional realtime plan crossings without recording tiers."""

    rules = [
        row
        for row in list_enabled_rules(connection)
        if row["type"] == DRAW_DOWN_PLAN_RULE_TYPE
    ]
    notifications: list[AlertNotification] = []
    no_data_skips: list[RuleNoDataSkip] = []
    errors: list[RuleCheckError] = []
    for rule in rules:
        try:
            rule_id = int(rule["id"])
            reference_symbol = str(rule["symbol"])
            prealert_exists = (
                connection.execute(
                    "SELECT 1 FROM alert_events WHERE alert_key = ? LIMIT 1",
                    (f"{rule_id}:drawdown_plan:pre_alert:{market_date.isoformat()}",),
                ).fetchone()
                is not None
            )
            name = str(rule["name"]).strip()
            if not name:
                raise ValueError("drawdown_plan name must not be empty.")
            config, active_cycle, recorded_tier_keys = _load_drawdown_plan_state(
                connection,
                rule,
            )
            if (
                prealert_exists
                and active_cycle is not None
                and active_cycle.last_evaluated_date >= confirmed_date
            ):
                continue
            history = _fetch_drawdown_plan_history(
                connection,
                rule,
                market_data_provider,
                end_date=confirmed_date,
            )
            if active_cycle is None:
                evaluate_drawdown_plan_rule(
                    connection,
                    rule,
                    history,
                    expected_date=confirmed_date,
                    initialize_only=True,
                )
                config, active_cycle, recorded_tier_keys = _load_drawdown_plan_state(
                    connection, rule
                )
            elif active_cycle.last_evaluated_date < confirmed_date:
                catch_up = evaluate_drawdown_plan_rule(
                    connection,
                    rule,
                    history,
                    expected_date=confirmed_date,
                    same_day_actions=False,
                )
                if catch_up.notification is not None:
                    notifications.append(catch_up.notification)
                config, active_cycle, recorded_tier_keys = _load_drawdown_plan_state(
                    connection, rule
                )
            confirmed = evaluate_drawdown_plan(
                history,
                config,
                reference_symbol=reference_symbol,
                expected_date=confirmed_date,
                active_cycle=active_cycle,
                recorded_tier_keys=recorded_tier_keys,
            )
            if prealert_exists:
                continue
            quote = market_data_provider.get_etf_realtime_quote(
                Instrument(reference_symbol, name, AssetType.CN_ETF)
            )
            try:
                realtime = evaluate_drawdown_plan_realtime(
                    confirmed,
                    config,
                    quote,
                    reference_symbol=reference_symbol,
                    market_date=market_date,
                    recorded_tier_keys=recorded_tier_keys,
                )
            except ValueError as eastmoney_validation_error:
                if quote.source != "eastmoney":
                    raise
                try:
                    quote = market_data_provider.get_sina_etf_realtime_quote(
                        Instrument(reference_symbol, name, AssetType.CN_ETF)
                    )
                except MarketDataProviderError as sina_fetch_error:
                    raise MarketDataFetchError(
                        "Eastmoney realtime quote rejected "
                        f"({eastmoney_validation_error}); Sina fallback unavailable "
                        f"({sina_fetch_error})."
                    ) from sina_fetch_error
                try:
                    realtime = evaluate_drawdown_plan_realtime(
                        confirmed,
                        config,
                        quote,
                        reference_symbol=reference_symbol,
                        market_date=market_date,
                        recorded_tier_keys=recorded_tier_keys,
                    )
                except ValueError as sina_validation_error:
                    raise MarketDataFetchError(
                        "Eastmoney realtime quote rejected "
                        f"({eastmoney_validation_error}); Sina fallback quote "
                        f"rejected ({sina_validation_error})."
                    ) from sina_validation_error
            if active_cycle is None:
                raise sqlite3.IntegrityError("Drawdown plan cycle was not initialized.")
            cycle_id = active_cycle.cycle_id
            added_tier_keys, skipped_tier_keys, snoozed_tier_keys = (
                _drawdown_reminder_key_sets(
                    connection,
                    cycle_id,
                    market_date=market_date,
                )
            )
            actionable_tiers = select_actionable_tiers(
                config=config,
                current_drawdown=realtime.drawdown,
                added_tier_keys=added_tier_keys,
                skipped_tier_keys=skipped_tier_keys,
                snoozed_tier_keys=snoozed_tier_keys,
            )
            actionable_keys = {tier.key for tier in actionable_tiers}
            newly_realtime_tiers = tuple(
                tier
                for tier in realtime.newly_crossed_tiers
                if tier.key in actionable_keys
            )
            newly_keys = {tier.key for tier in newly_realtime_tiers}
            pending_tiers = tuple(
                tier for tier in actionable_tiers if tier.key not in newly_keys
            )
            alert = build_drawdown_plan_pre_alert(
                rule_id=rule_id,
                cycle_id=cycle_id,
                reference_symbol=reference_symbol,
                name=name,
                confirmed_date=confirmed_date,
                config=config,
                evaluation=realtime,
                quote=quote,
                actionable_tiers=actionable_tiers,
                pending_tiers=pending_tiers,
            )
            if alert is None:
                continue
            try:
                event_id = add_drawdown_plan_pre_alert_event(
                    connection,
                    rule_id=rule_id,
                    alert=alert,
                )
            except sqlite3.IntegrityError:
                continue
            LOGGER.info(
                "Drawdown plan pre-alert rule_id=%s cycle_id=%s symbol=%s "
                "evaluation_date=%s "
                "latest_price=%s peak_price=%s drawdown=%s "
                "newly_crossed_tiers=%s sma=%s distance_to_sma=%s sma_slope=%s "
                "actionable_tiers=%s alert_reserved=true",
                rule_id,
                cycle_id,
                reference_symbol,
                realtime.latest_date,
                realtime.latest_price,
                realtime.peak_price,
                realtime.drawdown,
                [tier.key for tier in newly_realtime_tiers],
                [tier.key for tier in actionable_tiers],
                realtime.sma,
                realtime.distance_to_sma,
                realtime.sma_slope,
            )
            notifications.append(
                AlertNotification(
                    event_id=event_id,
                    title=str(alert["title"]),
                    text=str(alert["message"]),
                    telegram_actions=_drawdown_plan_action_rows(
                        rule_id,
                        event_id,
                        actionable_tiers,
                    ),
                )
            )
        except MarketDataProviderError as exc:
            no_data_skips.append(_plan_no_data_skip(rule, exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(_plan_check_error(rule, exc))
    return DrawdownPlanCheckResult(
        checked_rules=len(rules),
        notifications=notifications,
        no_data_skips=no_data_skips,
        errors=errors,
    )


def process_manual_add_estimates(
    connection: Any,
    market_data_provider: MarketDataProvider,
    market_calendar: MarketCalendar,
    *,
    processing_date: date,
    nav_cache: dict[tuple[str, date], Any] | None = None,
    nav_errors: dict[tuple[str, date], Exception] | None = None,
) -> ManualAddSettlementResult:
    """Apply pending additions only from their exact effective dated NAV."""

    occurrences = list_pending_manual_add_estimates(connection)
    notifications: list[AlertNotification] = []
    no_data_skips: list[RuleNoDataSkip] = []
    errors: list[RuleCheckError] = []
    navs = {} if nav_cache is None else nav_cache
    cached_errors = {} if nav_errors is None else nav_errors
    for occurrence in occurrences:
        rule_id = int(occurrence["rule_id"])
        fund_symbol = str(occurrence["fund_symbol"])
        effective_date = date.fromisoformat(str(occurrence["effective_date"]))
        if processing_date <= effective_date:
            continue
        try:
            settings = get_fund_settings(connection, fund_symbol)
            position = get_position_snapshot(connection, fund_symbol)
            if (
                settings is not None
                and settings["position_sync_required_since"] is not None
            ) or (
                position is not None
                and position["position_sync_required_since"] is not None
            ):
                LOGGER.info(
                    "Manual add estimate deferred estimate_id=%s fund_symbol=%s "
                    "reason=position_sync_required",
                    occurrence["id"],
                    fund_symbol,
                )
                continue
            if not market_calendar.confirmed_status(effective_date):
                raise ValueError(
                    f"Effective date {effective_date} is not a confirmed open day."
                )
            nav_key = (fund_symbol, effective_date)
            if nav_key in cached_errors:
                raise cached_errors[nav_key]
            if nav_key not in navs:
                try:
                    navs[nav_key] = get_cached_or_fetch_fund_nav(
                        connection,
                        market_data_provider,
                        fund_symbol,
                        nav_date=effective_date,
                    )
                except MarketDataProviderError as exc:
                    cached_errors[nav_key] = exc
                    raise
            applied = apply_manual_add_estimate(
                connection,
                estimate_id=int(occurrence["id"]),
                nav=navs[nav_key],
            )
            if applied is not None:
                notifications.append(
                    AlertNotification(
                        event_id=int(applied["event_id"]),
                        title=str(applied["title"]),
                        text=str(applied["message"]),
                    )
                )
        except (MarketDataProviderError, MarketCalendarUnavailableError) as exc:
            no_data_skips.append(
                RuleNoDataSkip(rule_id, fund_symbol, str(exc), effective_date)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(
                RuleCheckError(rule_id, fund_symbol, str(exc), effective_date)
            )
    return ManualAddSettlementResult(
        checked_estimates=len(occurrences),
        notifications=notifications,
        no_data_skips=no_data_skips,
        errors=errors,
    )


def process_scheduled_dca_occurrences(
    connection: Any,
    market_data_provider: MarketDataProvider,
    market_calendar: MarketCalendar,
    *,
    processing_date: date,
    nav_cache: dict[tuple[str, date], Any] | None = None,
    nav_errors: dict[tuple[str, date], Exception] | None = None,
) -> ManualAddSettlementResult:
    """Resolve holidays and quietly apply pending fixed DCA estimates once."""

    occurrences = list_pending_scheduled_dca_occurrences(connection)
    no_data_skips: list[RuleNoDataSkip] = []
    errors: list[RuleCheckError] = []
    navs = {} if nav_cache is None else nav_cache
    cached_errors = {} if nav_errors is None else nav_errors
    calendar_status: dict[date, bool] = {}
    for occurrence in occurrences:
        rule_id = int(occurrence["rule_id"])
        fund_symbol = str(occurrence["fund_symbol"])
        failure_date = processing_date
        try:
            effective_text = occurrence["effective_date"]
            if effective_text is None:
                due_date = date.fromisoformat(str(occurrence["due_date"]))
                failure_date = due_date
                if str(occurrence["holiday_policy"]) == "skip":
                    if not market_calendar.confirmed_status(due_date):
                        skip_scheduled_dca_occurrence(
                            connection,
                            rule_id=rule_id,
                            due_date=due_date.isoformat(),
                        )
                        continue
                    effective_text = due_date.isoformat()
                    set_scheduled_dca_effective_date(
                        connection,
                        occurrence_id=int(occurrence["id"]),
                        effective_date=effective_text,
                    )
                candidate = due_date
                while candidate < processing_date:
                    if effective_text is not None:
                        break
                    if candidate not in calendar_status:
                        calendar_status[candidate] = market_calendar.confirmed_status(
                            candidate
                        )
                    if calendar_status[candidate]:
                        effective_text = candidate.isoformat()
                        set_scheduled_dca_effective_date(
                            connection,
                            occurrence_id=int(occurrence["id"]),
                            effective_date=effective_text,
                        )
                        break
                    candidate += timedelta(days=1)
            if effective_text is None:
                continue
            effective_date = date.fromisoformat(str(effective_text))
            failure_date = effective_date
            if processing_date <= effective_date:
                continue
            if effective_date not in calendar_status:
                calendar_status[effective_date] = market_calendar.confirmed_status(
                    effective_date
                )
            if not calendar_status[effective_date]:
                raise ValueError(
                    f"Effective date {effective_date} is not a confirmed open day."
                )
            position = get_position_snapshot(connection, fund_symbol)
            if position is None:
                LOGGER.info(
                    "Scheduled DCA deferred occurrence_id=%s fund_symbol=%s "
                    "reason=position_not_synced",
                    occurrence["id"],
                    fund_symbol,
                )
                continue
            settings = get_fund_settings(connection, fund_symbol)
            if (
                settings is not None
                and settings["position_sync_required_since"] is not None
            ) or position["position_sync_required_since"] is not None:
                LOGGER.info(
                    "Scheduled DCA deferred occurrence_id=%s fund_symbol=%s "
                    "reason=position_sync_required",
                    occurrence["id"],
                    fund_symbol,
                )
                continue
            nav_key = (fund_symbol, effective_date)
            if nav_key in cached_errors:
                raise cached_errors[nav_key]
            if nav_key not in navs:
                try:
                    navs[nav_key] = get_cached_or_fetch_fund_nav(
                        connection,
                        market_data_provider,
                        fund_symbol,
                        nav_date=effective_date,
                    )
                except MarketDataProviderError as exc:
                    cached_errors[nav_key] = exc
                    raise
            if apply_scheduled_dca_occurrence(
                connection,
                occurrence_id=int(occurrence["id"]),
                nav=navs[nav_key],
            ):
                LOGGER.info(
                    "Scheduled DCA applied rule_id=%s occurrence_id=%s "
                    "fund_symbol=%s due_date=%s nav_date=%s nav_source=%s "
                    "gross_amount=%s position_applied=true",
                    rule_id,
                    occurrence["id"],
                    fund_symbol,
                    occurrence["due_date"],
                    effective_date,
                    navs[nav_key].source,
                    occurrence["gross_amount"],
                )
        except (MarketDataProviderError, MarketCalendarUnavailableError) as exc:
            no_data_skips.append(
                RuleNoDataSkip(rule_id, fund_symbol, str(exc), failure_date)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(RuleCheckError(rule_id, fund_symbol, str(exc), failure_date))
    return ManualAddSettlementResult(
        checked_estimates=len(occurrences),
        notifications=[],
        no_data_skips=no_data_skips,
        errors=errors,
    )


def read_drawdown_plan_statuses(
    connection: Any,
    market_data_provider: MarketDataProvider,
    *,
    end_date: date | None = None,
    force_refresh: bool = False,
) -> DrawdownPlanStatusResult:
    """Calculate current plan state without reserving alerts or changing cycles."""

    check_date = end_date or date.today()
    rules = [
        row
        for row in list_enabled_rules(connection)
        if row["type"] == DRAW_DOWN_PLAN_RULE_TYPE
    ]
    statuses: list[DrawdownPlanStatus] = []
    no_data_skips: list[RuleNoDataSkip] = []
    errors: list[RuleCheckError] = []
    for rule in rules:
        try:
            config, active_cycle, recorded_tier_keys = _load_drawdown_plan_state(
                connection,
                rule,
            )
            history = _fetch_drawdown_plan_history(
                connection,
                rule,
                market_data_provider,
                end_date=check_date,
                require_end_date=False,
                force_refresh=force_refresh,
                persist_end_date=check_date - timedelta(days=1),
            )
            latest_date = _latest_history_date(history)
            if latest_date is None:
                raise EmptyMarketDataError("Confirmed ETF history has no dated row.")
            evaluation = evaluate_drawdown_plan(
                history,
                config,
                reference_symbol=str(rule["symbol"]),
                expected_date=latest_date,
                active_cycle=active_cycle,
                recorded_tier_keys=recorded_tier_keys,
            )
            position = get_position_snapshot(
                connection,
                config.investment_fund_symbol,
            )
            readiness, missing_setup = derive_plan_readiness(
                connection,
                config.investment_fund_symbol,
            )
            settings = get_fund_settings(
                connection,
                config.investment_fund_symbol,
            )
            added_tier_keys = (
                frozenset()
                if active_cycle is None or evaluation.cycle_changed
                else frozenset(
                    str(row["tier_key"])
                    for row in list_manual_add_actions(
                        connection,
                        active_cycle.cycle_id,
                    )
                )
            )
            fund_nav = None
            if position is not None and float(position["units"]) > 0:
                try:
                    fund_nav = get_cached_or_fetch_fund_nav(
                        connection,
                        market_data_provider,
                        config.investment_fund_symbol,
                        force_refresh=force_refresh,
                    )
                except MarketDataProviderError as exc:
                    no_data_skips.append(
                        RuleNoDataSkip(
                            int(rule["id"]),
                            config.investment_fund_symbol,
                            str(exc),
                        )
                    )
            statuses.append(
                DrawdownPlanStatus(
                    rule_id=int(rule["id"]),
                    reference_symbol=str(rule["symbol"]),
                    name=str(rule["name"]),
                    config=config,
                    evaluation=evaluation,
                    recorded_tier_keys=frozenset(
                        () if evaluation.cycle_changed else recorded_tier_keys
                    ),
                    added_tier_keys=added_tier_keys,
                    readiness=readiness,
                    missing_setup=missing_setup,
                    position=position,
                    fund_nav=fund_nav,
                    position_sync_required_since=(
                        None
                        if settings is None
                        else settings["position_sync_required_since"]
                    ),
                )
            )
        except MarketDataProviderError as exc:
            no_data_skips.append(_plan_no_data_skip(rule, exc))
        except Exception as exc:  # noqa: BLE001
            errors.append(_plan_check_error(rule, exc))
    return DrawdownPlanStatusResult(len(rules), statuses, no_data_skips, errors)


def derive_plan_readiness(
    connection: Any,
    fund_symbol: str,
) -> tuple[str, tuple[str, ...]]:
    """Derive readiness from the shared fee and an explicit position snapshot."""

    settings = get_fund_settings(connection, fund_symbol)
    position = get_position_snapshot(connection, fund_symbol)
    missing = tuple(
        item
        for item, absent in (
            ("fund fee", settings is None or settings["fee_mode"] is None),
            ("position snapshot", position is None),
            (
                "position sync required",
                settings is not None
                and settings["position_sync_required_since"] is not None,
            ),
        )
        if absent
    )
    return ("READY" if not missing else "SETUP_REQUIRED"), missing


def _drawdown_plan_action_rows(
    rule_id: int,
    event_id: int,
    tiers: tuple[DrawdownTier, ...],
) -> tuple[tuple[tuple[str, str], ...], ...]:
    rows: list[tuple[tuple[str, str], ...]] = [
        (("✅ 已按全部档位加仓", f"drawdown_add:{rule_id}:{event_id}:all"),)
    ]
    if len(tiers) > 1:
        rows.extend(
            (
                (
                    "仅记录 "
                    f"-{format_plan_percent(tier.drawdown)} "
                    f"{format_plan_amount(tier.amount)}",
                    f"drawdown_add:{rule_id}:{event_id}:tier:{index}",
                ),
            )
            for index, tier in enumerate(tiers)
        )
    rows.append((("⏭ 暂未加仓", f"drawdown_add:{rule_id}:{event_id}:none"),))
    return tuple(rows)


def _load_drawdown_plan_state(
    connection: Any,
    rule: Any,
) -> tuple[DrawdownPlanConfig, ActiveDrawdownCycle | None, set[str]]:
    config = parse_drawdown_plan_config(
        reference_symbol=str(rule["symbol"]),
        asset_type=str(rule["asset_type"]),
        params=_load_params(str(rule["params_json"])),
    )
    validate_drawdown_plan_notification_size(
        name=str(rule["name"]),
        reference_symbol=str(rule["symbol"]),
        config=config,
    )
    active_row = get_active_drawdown_cycle(connection, int(rule["id"]))
    if active_row is None:
        return config, None, set()
    active_cycle = ActiveDrawdownCycle(
        cycle_id=int(active_row["id"]),
        peak_date=date.fromisoformat(str(active_row["peak_date"])),
        peak_price=float(active_row["peak_price"]),
        last_evaluated_date=date.fromisoformat(str(active_row["last_evaluated_date"])),
    )
    recorded = {
        str(row["tier_key"])
        for row in list_drawdown_tier_records(
            connection,
            active_cycle.cycle_id,
            source="close_confirmed",
        )
    }
    return config, active_cycle, recorded


def _drawdown_reminder_key_sets(
    connection: Any,
    cycle_id: int,
    *,
    market_date: date,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Load added, cycle-skipped, and current-date snoozed tier keys."""

    added = frozenset(
        str(row["tier_key"]) for row in list_manual_add_actions(connection, cycle_id)
    )
    states = get_drawdown_tier_reminder_states(connection, cycle_id)
    skipped = frozenset(
        key for key, row in states.items() if bool(row["skipped_for_cycle"])
    )
    snoozed = frozenset(
        key
        for key, row in states.items()
        if row["snoozed_market_date"] == market_date.isoformat()
    )
    return added, skipped, snoozed


_HISTORY_OVERLAP_DAYS = 5
_HISTORY_COVERAGE_BUFFER_DAYS = 14


def _history_frame_from_rows(
    rows: list[Any],
    *,
    symbol: str,
    asset_type: AssetType,
    price_basis: PriceBasis,
) -> pd.DataFrame | None:
    if not rows:
        return None
    frame = pd.DataFrame([dict(row) for row in rows])
    if frame.empty or "date" not in frame.columns or "close" not in frame.columns:
        return None
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    if frame.empty:
        return None
    for column in NORMALIZED_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    frame = (
        frame[NORMALIZED_COLUMNS]
        .sort_values("date", ascending=True, kind="stable")
        .drop_duplicates("date", keep="last")
        .reset_index(drop=True)
    )
    sources = {
        str(value).strip()
        for value in frame["source"].dropna().tolist()
        if str(value).strip()
    }
    frame.attrs.update(
        {
            "symbol": symbol,
            "source": next(iter(sources), "") if len(sources) == 1 else "",
            "price_basis": price_basis.value,
            "frequency": "daily",
        }
    )
    return frame


def _earliest_history_date(history: pd.DataFrame | None) -> date | None:
    if history is None or history.empty or "date" not in history.columns:
        return None
    dates = pd.to_datetime(history["date"], errors="coerce").dropna()
    return None if dates.empty else dates.min().date()


def _persist_history(
    connection: Any,
    history: pd.DataFrame,
    *,
    symbol: str,
    asset_type: AssetType,
    price_basis: PriceBasis,
) -> None:
    if history is None or history.empty:
        return
    source = str(history.attrs.get("source", "")).strip()
    rows = []
    for row in history.to_dict("records"):
        row.setdefault("source", source)
        rows.append(row)
    upsert_market_history(
        connection,
        symbol=symbol,
        asset_type=asset_type.value,
        price_basis=price_basis.value,
        rows=rows,
    )


def _history_with_persistent_cache(
    connection: Any,
    market_data_provider: MarketDataProvider,
    instrument: Instrument,
    *,
    start_date: date,
    end_date: date,
    price_basis: PriceBasis,
    require_end_date: bool,
    force_refresh: bool = False,
    cache_end_date: date | None = None,
    persist_end_date: date | None = None,
    return_fetched_if_persisted: bool = False,
    required_end_date: date | None = None,
) -> pd.DataFrame:
    """Read confirmed history from SQLite and fetch only missing ranges."""

    symbol = str(instrument.symbol)
    asset_type = AssetType(instrument.asset_type)
    storage_end_date = cache_end_date or end_date
    storage_start_date = start_date - timedelta(days=_HISTORY_COVERAGE_BUFFER_DAYS)
    cached_coverage = _history_frame_from_rows(
        load_market_history(
            connection,
            symbol=symbol,
            asset_type=asset_type.value,
            price_basis=price_basis.value,
            start_date=storage_start_date,
            end_date=storage_end_date,
        ),
        symbol=symbol,
        asset_type=asset_type,
        price_basis=price_basis,
    )
    earliest = _earliest_history_date(cached_coverage)
    cached = cached_coverage
    latest = None if cached is None else _latest_history_date(cached)
    coverage_end_date = required_end_date or end_date
    covered = (
        cached_coverage is not None
        and cached is not None
        and not cached.empty
        and earliest is not None
        and earliest <= start_date
        and (
            not require_end_date or (latest is not None and latest >= coverage_end_date)
        )
    )
    if covered and not force_refresh:
        return cached

    fetch_start = storage_start_date
    if (
        price_basis is not PriceBasis.QFQ
        and earliest is not None
        and earliest <= start_date
        and latest is not None
        and latest >= start_date
    ):
        fetch_start = max(
            storage_start_date,
            latest - timedelta(days=_HISTORY_OVERLAP_DAYS),
        )
    fetched_history = market_data_provider.get_history(
        instrument,
        fetch_start,
        end_date,
        price_basis=price_basis,
    )
    history = fetched_history
    if persist_end_date is not None:
        history = history.loc[
            pd.to_datetime(history["date"], errors="coerce")
            <= pd.Timestamp(persist_end_date)
        ].copy()
    _persist_history(
        connection,
        history,
        symbol=symbol,
        asset_type=asset_type,
        price_basis=price_basis,
    )
    merged = _history_frame_from_rows(
        load_market_history(
            connection,
            symbol=symbol,
            asset_type=asset_type.value,
            price_basis=price_basis.value,
            start_date=storage_start_date,
            end_date=storage_end_date,
        ),
        symbol=symbol,
        asset_type=asset_type,
        price_basis=price_basis,
    )
    merged_latest = None if merged is None else _latest_history_date(merged)
    if required_end_date is not None and (
        merged_latest is None or merged_latest < required_end_date
    ):
        raise EmptyMarketDataError(
            f"No confirmed history available through {required_end_date} "
            f"for {instrument.symbol}."
        )
    if merged is None and persist_end_date is not None:
        if not return_fetched_if_persisted:
            raise EmptyMarketDataError(
                f"No confirmed history available for {instrument.symbol}."
            )
    if return_fetched_if_persisted and persist_end_date is not None:
        return fetched_history
    return fetched_history if merged is None else merged


def get_cached_or_fetch_fund_nav(
    connection: Any,
    market_data_provider: MarketDataProvider,
    fund_symbol: str,
    *,
    nav_date: date | None = None,
    force_refresh: bool = False,
) -> FundNav:
    """Return cached NAV first; fetch and persist only when it is missing."""

    if not force_refresh:
        cached = get_cached_fund_nav(connection, fund_symbol, nav_date)
        if cached is not None:
            return FundNav(
                symbol=str(cached["fund_symbol"]),
                date=date.fromisoformat(str(cached["nav_date"])),
                value=float(cached["unit_nav"]),
                source=str(cached["source"]),
            )
    nav = market_data_provider.get_fund_nav(
        Instrument(fund_symbol, fund_symbol, AssetType.CN_OPEN_FUND),
        nav_date=nav_date,
    )
    upsert_fund_nav(
        connection,
        fund_symbol=str(nav.symbol),
        nav_date=nav.date,
        unit_nav=nav.value,
        source=nav.source,
    )
    return nav


def _fetch_drawdown_plan_history(
    connection: Any,
    rule: Any,
    market_data_provider: MarketDataProvider,
    *,
    end_date: date,
    require_end_date: bool = True,
    force_refresh: bool = False,
    persist_end_date: date | None = None,
    return_fetched_if_persisted: bool = False,
) -> pd.DataFrame:
    config, active_cycle, _recorded = _load_drawdown_plan_state(connection, rule)
    instrument = Instrument(
        symbol=str(rule["symbol"]),
        name=str(rule["name"]),
        asset_type=AssetType.CN_ETF,
    )
    return _history_with_persistent_cache(
        connection,
        market_data_provider,
        instrument,
        start_date=required_history_start(
            evaluation_date=end_date,
            config=config,
            active_peak_date=None if active_cycle is None else active_cycle.peak_date,
        ),
        end_date=end_date,
        price_basis=PriceBasis.QFQ,
        require_end_date=require_end_date,
        force_refresh=force_refresh,
        persist_end_date=persist_end_date,
        return_fetched_if_persisted=return_fetched_if_persisted,
    )


def _plan_no_data_skip(rule: Any, exc: Exception) -> RuleNoDataSkip:
    return RuleNoDataSkip(int(rule["id"]), str(rule["symbol"]), str(exc))


def _plan_check_error(rule: Any, exc: Exception) -> RuleCheckError:
    return RuleCheckError(int(rule["id"]), str(rule["symbol"]), str(exc))


def evaluate_drawdown_rules(
    connection: Any,
    market_data_provider: MarketDataProvider,
    *,
    today: date | None = None,
    require_new_data_date: date | None = None,
    include_latest: bool = False,
    confirmed_end_date: date | None = None,
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
        connection=connection,
        market_data_provider=market_data_provider,
        end_date=end_date,
        include_latest=include_latest,
        confirmed_end_date=confirmed_end_date,
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
    *,
    evaluation_date: date | None = None,
    market_calendar: MarketCalendar | None = None,
) -> ProfitCheckResult:
    """Evaluate enabled profit reminder rules and store new alert events."""

    rules = [
        row for row in list_enabled_rules(connection) if row["type"] == PROFIT_RULE_TYPE
    ]

    notifications: list[AlertNotification] = []
    errors: list[RuleCheckError] = []
    no_data_skips: list[RuleNoDataSkip] = []
    skipped_duplicates = 0
    checked_rules = len(rules)
    latest_cache: dict[MarketDataCacheKey, dict[str, object] | None] = {}
    expected_market_data_date: date | None = None
    market_data_date_loaded = False
    expected_fund_data_date: date | None = None
    fund_data_date_loaded = False

    for row in rules:
        try:
            if _load_params(str(row["params_json"])).get("cost") == "auto":
                checked_rules -= 1
                continue
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

            if evaluation_date is not None:
                if asset_type is AssetType.CN_OPEN_FUND:
                    if market_calendar is None:
                        raise MarketCalendarUnavailableError(
                            "CN trade calendar is required to validate fund data."
                        )
                    if not fund_data_date_loaded:
                        expected_fund_data_date = latest_completed_open_date(
                            market_calendar,
                            evaluation_date,
                        )
                        fund_data_date_loaded = True
                    _validate_profit_latest_date(
                        latest,
                        symbol=str(row["symbol"]),
                        evaluation_date=evaluation_date,
                        earliest_open_date=expected_fund_data_date,
                    )
                else:
                    if not market_data_date_loaded:
                        expected_market_data_date = _latest_market_data_date(
                            market_calendar,
                            evaluation_date,
                        )
                        market_data_date_loaded = True
                    _validate_profit_latest_date(
                        latest,
                        symbol=str(row["symbol"]),
                        evaluation_date=evaluation_date,
                        expected_date=expected_market_data_date,
                    )

            alerts = build_profit_alerts(
                row,
                latest,
                lambda alert_key: alert_exists(connection, alert_key),
            )
        except (LatestDataUnavailableError, MarketCalendarUnavailableError) as exc:
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
        checked_rules=checked_rules,
        notifications=notifications,
        skipped_duplicates=skipped_duplicates,
        no_data_skips=no_data_skips,
        errors=errors,
    )


def _validate_profit_latest_date(
    latest: dict[str, object],
    *,
    symbol: str,
    evaluation_date: date,
    expected_date: date | None = None,
    earliest_open_date: date | None = None,
) -> None:
    """Reject missing or stale latest rows before a fixed-cost alert is built."""

    parsed_date = pd.to_datetime(latest.get("date"), errors="coerce")
    if pd.isna(parsed_date):
        raise LatestDataUnavailableError(
            f"Latest market data date is unavailable for {symbol}."
        )
    actual_date = parsed_date.date()
    expected_date = expected_date or evaluation_date
    valid = (
        earliest_open_date <= actual_date <= evaluation_date
        if earliest_open_date is not None
        else actual_date == expected_date
    )
    if not valid:
        expected = (
            f"{earliest_open_date.isoformat()}..{evaluation_date.isoformat()}"
            if earliest_open_date is not None
            else expected_date.isoformat()
        )
        raise LatestDataUnavailableError(
            f"Latest market data for {symbol} is stale: "
            f"data date={actual_date.isoformat()}, expected={expected}."
        )


def _latest_market_data_date(
    market_calendar: MarketCalendar | None,
    processing_date: date,
) -> date:
    """Return the freshest valid close date for a market-data evaluation."""

    if market_calendar is None:
        return processing_date
    if market_calendar.confirmed_status(processing_date):
        return processing_date
    return latest_completed_open_date(market_calendar, processing_date)


def latest_completed_open_date(
    market_calendar: MarketCalendar,
    processing_date: date,
) -> date:
    """Return the latest confirmed CN open day strictly before processing."""

    candidate = processing_date - timedelta(days=1)
    for _ in range(366):
        if market_calendar.confirmed_status(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise MarketCalendarUnavailableError("No completed CN open day was found.")


def position_profit_action_rows(
    event_id: int,
) -> tuple[tuple[tuple[str, str], ...], ...]:
    return (
        (("✅ Partially redeemed", f"profit_action:{event_id}:partial"),),
        (("✅ Fully closed", f"profit_action:{event_id}:close"),),
        (("⏭ No action", f"profit_action:{event_id}:none"),),
    )


def _evaluate_cached_position_profit_nav(
    connection: Any,
    row: Any,
    nav: Any,
    *,
    nav_date: str,
) -> AlertNotification | None:
    rule_id = int(row["id"])
    symbol = str(row["symbol"])
    position = get_position_snapshot(connection, symbol)
    cycle = get_active_position_cycle(connection, symbol)
    settings = get_fund_settings(connection, symbol)
    if (
        position is None
        or float(position["units"]) <= 0
        or cycle is None
        or position["position_sync_required_since"] is not None
        or (
            settings is not None
            and settings["position_sync_required_since"] is not None
        )
    ):
        return None
    cycle_id = int(cycle["id"])
    recorded_threshold_keys = list_position_profit_threshold_keys(
        connection,
        rule_id=rule_id,
        position_cycle_id=cycle_id,
    )
    configured_thresholds = _load_params(str(row["params_json"]))["thresholds"]
    if len(recorded_threshold_keys) >= len(configured_thresholds) or (
        has_position_profit_evaluation(
            connection,
            rule_id=rule_id,
            position_cycle_id=cycle_id,
            nav_date=nav_date,
        )
    ):
        return None
    built = build_position_profit_alert(
        row,
        nav,
        position,
        position_cycle_id=cycle_id,
        recorded_threshold_keys=recorded_threshold_keys,
    )
    if built is None:
        record_position_profit_evaluation(
            connection,
            rule_id=rule_id,
            position_cycle_id=cycle_id,
            nav_date=nav_date,
            position=position,
        )
        return None
    alert, thresholds = built
    event_id = persist_position_profit_alert(
        connection,
        rule_id=rule_id,
        position_cycle_id=cycle_id,
        alert=alert,
        thresholds=thresholds,
        nav_date=nav_date,
    )
    LOGGER.info(
        "Position-linked Price-Gain reserved rule_id=%s symbol=%s "
        "evaluation_date=%s unit_nav=%s average_unit_cost=%s "
        "new_thresholds=%s alert_event_id=%s",
        rule_id,
        symbol,
        alert["payload"]["nav_date"],
        alert["payload"]["unit_nav"],
        alert["payload"]["average_unit_cost"],
        [key for key, _value in thresholds],
        event_id,
    )
    return AlertNotification(
        event_id=event_id,
        title=str(alert["title"]),
        text=str(alert["message"]),
        telegram_actions=position_profit_action_rows(event_id),
    )


def evaluate_position_profit_rules(
    connection: Any,
    market_data_provider: MarketDataProvider,
    market_calendar: MarketCalendar,
    *,
    processing_date: date,
    nav_cache: dict[tuple[str, date], Any] | None = None,
    nav_errors: dict[tuple[str, date], Exception] | None = None,
) -> ProfitCheckResult:
    """Evaluate auto-cost Price-Gain rules from one exact completed fund NAV."""

    rules = []
    errors: list[RuleCheckError] = []
    for row in list_enabled_rules(connection):
        if (
            row["type"] != PROFIT_RULE_TYPE
            or row["asset_type"] != AssetType.CN_OPEN_FUND.value
        ):
            continue
        try:
            if _load_params(str(row["params_json"])).get("cost") == "auto":
                rules.append(row)
        except Exception as exc:  # noqa: BLE001
            errors.append(RuleCheckError(int(row["id"]), str(row["symbol"]), str(exc)))
    notifications: list[AlertNotification] = []
    no_data_skips: list[RuleNoDataSkip] = []
    navs = {} if nav_cache is None else nav_cache
    cached_errors = {} if nav_errors is None else nav_errors
    try:
        expected_date = latest_completed_open_date(market_calendar, processing_date)
    except MarketCalendarUnavailableError as exc:
        return ProfitCheckResult(
            checked_rules=len(rules),
            notifications=[],
            skipped_duplicates=0,
            no_data_skips=[
                RuleNoDataSkip(int(row["id"]), str(row["symbol"]), str(exc))
                for row in rules
            ],
            errors=errors,
        )
    errors = [
        RuleCheckError(error.rule_id, error.symbol, error.message, expected_date)
        for error in errors
    ]
    for row in rules:
        rule_id = int(row["id"])
        symbol = str(row["symbol"])
        try:
            position = get_position_snapshot(connection, symbol)
            cycle = get_active_position_cycle(connection, symbol)
            if position is None or float(position["units"]) <= 0 or cycle is None:
                LOGGER.info(
                    "Position-linked Price-Gain skipped rule_id=%s symbol=%s "
                    "reason=positive_position_required",
                    rule_id,
                    symbol,
                )
                continue
            settings = get_fund_settings(connection, symbol)
            if (
                settings is not None
                and settings["position_sync_required_since"] is not None
            ) or position["position_sync_required_since"] is not None:
                LOGGER.info(
                    "Position-linked Price-Gain skipped rule_id=%s symbol=%s "
                    "reason=position_sync_required",
                    rule_id,
                    symbol,
                )
                continue
            cycle_id = int(cycle["id"])
            recorded_threshold_keys = list_position_profit_threshold_keys(
                connection,
                rule_id=rule_id,
                position_cycle_id=cycle_id,
            )
            configured_thresholds = _load_params(str(row["params_json"]))["thresholds"]
            if len(recorded_threshold_keys) >= len(configured_thresholds):
                continue
            nav_date = expected_date.isoformat()
            if has_position_profit_evaluation(
                connection,
                rule_id=rule_id,
                position_cycle_id=cycle_id,
                nav_date=nav_date,
            ):
                continue
            nav_key = (symbol, expected_date)
            if nav_key in cached_errors:
                raise cached_errors[nav_key]
            if nav_key not in navs:
                try:
                    navs[nav_key] = get_cached_or_fetch_fund_nav(
                        connection,
                        market_data_provider,
                        symbol,
                        nav_date=expected_date,
                    )
                except MarketDataProviderError as exc:
                    cached_errors[nav_key] = exc
                    raise
            if (
                navs[nav_key].date != expected_date
                or str(navs[nav_key].source) != "akshare_eastmoney"
            ):
                raise MarketDataProviderError(
                    f"Exact Eastmoney fund NAV for {expected_date} is unavailable."
                )
            for attempt in range(2):
                try:
                    notification = _evaluate_cached_position_profit_nav(
                        connection,
                        row,
                        navs[nav_key],
                        nav_date=nav_date,
                    )
                    if notification is not None:
                        notifications.append(notification)
                    break
                except sqlite3.IntegrityError:
                    if attempt:
                        raise
        except (MarketDataProviderError, MarketCalendarUnavailableError) as exc:
            no_data_skips.append(
                RuleNoDataSkip(rule_id, symbol, str(exc), expected_date)
            )
        except sqlite3.IntegrityError:
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(RuleCheckError(rule_id, symbol, str(exc), expected_date))
    return ProfitCheckResult(
        checked_rules=len(rules),
        notifications=notifications,
        skipped_duplicates=0,
        no_data_skips=no_data_skips,
        errors=errors,
        data_date=expected_date,
    )


def evaluate_dca_rules(
    connection: Any,
    *,
    today: date | None = None,
    market_calendar: MarketCalendar | None = None,
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
            occurrence = None
            if str(row["asset_type"]) == AssetType.CN_OPEN_FUND.value:
                if market_calendar is None:
                    raise ValueError("Confirmed CN market calendar is required.")
                params = _load_params(str(row["params_json"]))
                if normalize_weekday(str(params["weekday"])) != weekday_for_date(
                    check_date
                ):
                    continue
                policy = str(params.get("holiday_policy", "next"))
                try:
                    open_due_date = market_calendar.confirmed_status(check_date)
                except MarketCalendarUnavailableError:
                    open_due_date = None
                occurrence = create_scheduled_dca_occurrence(
                    connection,
                    rule_id=int(row["id"]),
                    fund_symbol=str(row["symbol"]),
                    due_date=check_date.isoformat(),
                    gross_amount=float(params["amount"]),
                    holiday_policy=policy,
                    effective_date=(check_date.isoformat() if open_due_date else None),
                    skipped=open_due_date is False and policy == "skip",
                )
                LOGGER.info(
                    "Scheduled DCA occurrence rule_id=%s fund_symbol=%s "
                    "due_date=%s effective_date=%s status=%s gross_amount=%s",
                    row["id"],
                    row["symbol"],
                    check_date,
                    occurrence["effective_date"],
                    occurrence["status"],
                    occurrence["gross_amount"],
                )
            alert = build_dca_reminder_alert(
                row,
                check_date,
                lambda alert_key: alert_exists(connection, alert_key),
                occurrence_status=(
                    None if occurrence is None else str(occurrence["status"])
                ),
                effective_date=(
                    None if occurrence is None else occurrence["effective_date"]
                ),
                occurrence_amount=(
                    None if occurrence is None else float(occurrence["gross_amount"])
                ),
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
                telegram_actions=(
                    (
                        (
                            f"⚠️ Deduction failed — {row['symbol']}",
                            f"dca_skip:{row['id']}:{check_date.isoformat()}",
                        ),
                    ),
                )
                if occurrence is not None and str(occurrence["status"]) == "pending"
                else (),
                dca_summary=(
                    build_dca_notification_summary(
                        message=str(alert["message"]),
                        due_date=check_date.isoformat(),
                        amount=float(alert["payload"]["amount"]),
                        skipped=str(occurrence["status"]) == "skipped",
                    )
                    if occurrence is not None
                    else None
                ),
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
        connection: Any | None = None,
        market_data_provider: MarketDataProvider,
        end_date: date,
        include_latest: bool,
        confirmed_end_date: date | None = None,
    ) -> None:
        self._connection = connection
        self._market_data_provider = market_data_provider
        self._end_date = end_date
        self._include_latest = include_latest
        self._confirmed_end_date = confirmed_end_date
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

        if self._include_latest and context.cache_key not in self._latest_cache:
            self._latest_cache[context.cache_key] = (
                self._market_data_provider.get_latest(context.instrument)
            )
        history = self._history_for(context)
        if not self._include_latest:
            return history

        if context.cache_key in self._combined_history_cache:
            return self._combined_history_cache[context.cache_key]

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
            if self._connection is not None:
                has_latest = self._latest_cache.get(context.cache_key) is not None
                history_end_date = self._end_date
                if (
                    self._include_latest
                    and self._confirmed_end_date is not None
                    and not has_latest
                ):
                    history_end_date = self._confirmed_end_date
                history = _history_with_persistent_cache(
                    self._connection,
                    self._market_data_provider,
                    context.instrument,
                    start_date=start_date,
                    end_date=history_end_date,
                    price_basis=context.cache_key.price_basis,
                    require_end_date=(
                        not self._include_latest
                        or (self._confirmed_end_date is not None and has_latest)
                    ),
                    cache_end_date=(
                        self._confirmed_end_date
                        if self._include_latest and self._confirmed_end_date is not None
                        else None
                    ),
                    persist_end_date=(
                        self._confirmed_end_date
                        if self._include_latest and self._confirmed_end_date is not None
                        else None
                    ),
                    required_end_date=(
                        self._confirmed_end_date if has_latest else None
                    ),
                    return_fetched_if_persisted=(
                        self._include_latest
                        and self._confirmed_end_date is not None
                        and not has_latest
                    ),
                )
            elif context.cache_key.price_basis is PriceBasis.UNADJUSTED:
                history = self._market_data_provider.get_history(
                    context.instrument,
                    start_date,
                    self._end_date,
                )
            else:
                history = self._market_data_provider.get_history(
                    context.instrument,
                    start_date,
                    self._end_date,
                    price_basis=context.cache_key.price_basis,
                )
            self._history_cache[context.cache_key] = history
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
