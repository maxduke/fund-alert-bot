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
    get_active_drawdown_cycle,
    get_fund_settings,
    get_position_snapshot,
    list_drawdown_tier_records,
    list_enabled_rules,
    list_manual_add_actions,
    list_pending_manual_add_estimates,
    persist_drawdown_plan_evaluation,
    reserve_alert_event,
)
from fund_alert_bot.market_data import (
    AssetType,
    EmptyMarketDataError,
    FundNav,
    Instrument,
    MarketCalendar,
    MarketCalendarUnavailableError,
    MarketDataProvider,
    MarketDataProviderError,
    PriceBasis,
)
from fund_alert_bot.rules.dca import build_dca_reminder_alert
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
)
from fund_alert_bot.rules.profit import (
    LatestDataUnavailableError,
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
class AlertNotification:
    """Alert text ready to send after the event has been reserved."""

    event_id: int
    title: str
    text: str
    telegram_actions: tuple[tuple[tuple[str, str], ...], ...] = ()


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
    lines = [
        "⚠️ Drawdown plan data unavailable",
        "",
        f"Data date: {evaluation_date.isoformat()}",
        f"{phase_label} could not evaluate:",
        *(f"• {item.symbol}: {item.message}" for item in affected),
        "",
        (
            "The recorded addition remains pending; no position estimate was applied."
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
            title="Drawdown plan data unavailable",
            message=message,
            payload={
                "phase": phase,
                "data_date": evaluation_date.isoformat(),
                "affected_plans": [
                    {
                        "rule_id": item.rule_id,
                        "symbol": item.symbol,
                        "reason": item.message,
                    }
                    for item in affected
                ],
            },
        )
    except sqlite3.IntegrityError:
        return None
    return AlertNotification(event_id=event_id, title="Data unavailable", text=message)


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
        connection,
        rule,
    )

    evaluation = evaluate_drawdown_plan(
        history,
        config,
        reference_symbol=reference_symbol,
        expected_date=expected_date,
        active_cycle=active_cycle,
        recorded_tier_keys=recorded_tier_keys,
    )
    alert = build_drawdown_plan_alert(
        rule_id=rule_id,
        reference_symbol=reference_symbol,
        name=name,
        config=config,
        evaluation=evaluation,
    )
    tiers = evaluation.newly_crossed_tiers
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
        tiers=tiers,
        alert=alert,
    )
    LOGGER.info(
        "Drawdown plan evaluation rule_id=%s cycle_id=%s symbol=%s evaluation_date=%s "
        "latest_price=%s peak_price=%s drawdown=%s newly_crossed_tiers=%s "
        "sma=%s distance_to_sma=%s sma_slope=%s alert_reserved=%s",
        rule_id,
        cycle_id,
        reference_symbol,
        evaluation.latest_date,
        evaluation.latest_price,
        evaluation.peak_price,
        evaluation.drawdown,
        [tier.key for tier in tiers],
        evaluation.sma,
        evaluation.distance_to_sma,
        evaluation.sma_slope,
        event_id is not None,
    )
    notification = None
    if event_id is not None and alert is not None:
        notification = AlertNotification(
            event_id=event_id,
            title=str(alert["title"]),
            text=str(alert["message"]),
            telegram_actions=_drawdown_plan_action_rows(rule_id, event_id, tiers),
        )
    return DrawdownPlanRuleResult(
        cycle_id=cycle_id,
        evaluation=evaluation,
        notification=notification,
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
            if (
                active_cycle is None
                or active_cycle.last_evaluated_date < confirmed_date
            ):
                catch_up = evaluate_drawdown_plan_rule(
                    connection,
                    rule,
                    history,
                    expected_date=confirmed_date,
                )
                if catch_up.notification is not None:
                    catch_up_text = catch_up.notification.text.split(
                        "\nOnly after you actually subscribe, record it with:",
                        maxsplit=1,
                    )[0]
                    notifications.append(
                        AlertNotification(
                            event_id=catch_up.notification.event_id,
                            title=catch_up.notification.title,
                            text=(
                                f"{catch_up_text}\n"
                                "Delayed confirmation for the previous trading day; "
                                "action buttons are unavailable.\n"
                                "If you bought, wait for the fund platform to settle, "
                                "then run /sync_position.\n"
                                "This is a reminder only. No trade has been placed."
                            ),
                        )
                    )
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
            except ValueError:
                if quote.source != "eastmoney":
                    raise
                quote = market_data_provider.get_sina_etf_realtime_quote(
                    Instrument(reference_symbol, name, AssetType.CN_ETF)
                )
                realtime = evaluate_drawdown_plan_realtime(
                    confirmed,
                    config,
                    quote,
                    reference_symbol=reference_symbol,
                    market_date=market_date,
                    recorded_tier_keys=recorded_tier_keys,
                )
            if active_cycle is None:
                raise sqlite3.IntegrityError("Drawdown plan cycle was not initialized.")
            cycle_id = active_cycle.cycle_id
            alert = build_drawdown_plan_pre_alert(
                rule_id=rule_id,
                cycle_id=cycle_id,
                reference_symbol=reference_symbol,
                name=name,
                confirmed_date=confirmed_date,
                config=config,
                evaluation=realtime,
                quote=quote,
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
                "alert_reserved=true",
                rule_id,
                cycle_id,
                reference_symbol,
                realtime.latest_date,
                realtime.latest_price,
                realtime.peak_price,
                realtime.drawdown,
                [tier.key for tier in realtime.newly_crossed_tiers],
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
                        realtime.newly_crossed_tiers,
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
) -> ManualAddSettlementResult:
    """Apply pending additions only from their exact effective dated NAV."""

    occurrences = list_pending_manual_add_estimates(connection)
    notifications: list[AlertNotification] = []
    no_data_skips: list[RuleNoDataSkip] = []
    errors: list[RuleCheckError] = []
    navs: dict[tuple[str, date], Any] = {}
    for occurrence in occurrences:
        rule_id = int(occurrence["rule_id"])
        fund_symbol = str(occurrence["fund_symbol"])
        effective_date = date.fromisoformat(str(occurrence["effective_date"]))
        if processing_date <= effective_date:
            continue
        try:
            if not market_calendar.confirmed_status(effective_date):
                raise ValueError(
                    f"Effective date {effective_date} is not a confirmed open day."
                )
            nav_key = (fund_symbol, effective_date)
            if nav_key not in navs:
                navs[nav_key] = market_data_provider.get_fund_nav(
                    Instrument(fund_symbol, fund_symbol, AssetType.CN_OPEN_FUND),
                    nav_date=effective_date,
                )
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
            no_data_skips.append(RuleNoDataSkip(rule_id, fund_symbol, str(exc)))
        except Exception as exc:  # noqa: BLE001
            errors.append(RuleCheckError(rule_id, fund_symbol, str(exc)))
    return ManualAddSettlementResult(
        checked_estimates=len(occurrences),
        notifications=notifications,
        no_data_skips=no_data_skips,
        errors=errors,
    )


def read_drawdown_plan_statuses(
    connection: Any,
    market_data_provider: MarketDataProvider,
    *,
    end_date: date | None = None,
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
                    fund_nav = market_data_provider.get_fund_nav(
                        Instrument(
                            config.investment_fund_symbol,
                            str(rule["name"]),
                            AssetType.CN_OPEN_FUND,
                        )
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
        for row in list_drawdown_tier_records(connection, active_cycle.cycle_id)
    }
    return config, active_cycle, recorded


def _fetch_drawdown_plan_history(
    connection: Any,
    rule: Any,
    market_data_provider: MarketDataProvider,
    *,
    end_date: date,
) -> pd.DataFrame:
    config, active_cycle, _recorded = _load_drawdown_plan_state(connection, rule)
    instrument = Instrument(
        symbol=str(rule["symbol"]),
        name=str(rule["name"]),
        asset_type=AssetType.CN_ETF,
    )
    return market_data_provider.get_history(
        instrument,
        required_history_start(
            evaluation_date=end_date,
            config=config,
            active_peak_date=None if active_cycle is None else active_cycle.peak_date,
        ),
        end_date,
        price_basis=PriceBasis.QFQ,
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
            if context.cache_key.price_basis is PriceBasis.UNADJUSTED:
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
