"""Scheduler wiring."""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import date, datetime, time, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fund_alert_bot.checks import (
    AlertNotification,
    DrawdownCheckResult,
    DrawdownPlanCheckResult,
    ManualAddSettlementResult,
    RuleNoDataSkip,
    build_dca_notification_summary,
    evaluate_dca_rules,
    evaluate_drawdown_plan_prealerts,
    evaluate_drawdown_plan_rules,
    evaluate_drawdown_rules,
    evaluate_position_profit_rules,
    evaluate_profit_rules,
    format_delayed_drawdown_plan_message,
    latest_completed_open_date,
    position_profit_action_rows,
    process_manual_add_estimates,
    process_scheduled_dca_occurrences,
    reserve_drawdown_plan_data_unavailable_notice,
)
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import (
    initialize_database,
    list_enabled_rules,
    list_retryable_drawdown_plan_alert_events,
    list_retryable_position_profit_alert_events,
    list_retryable_standard_alert_events,
    open_connection,
)
from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    CNMarketCalendar,
    MarketCalendar,
    MarketCalendarUnavailableError,
    MarketDataProvider,
)
from fund_alert_bot.notifications.dispatch import send_alert_notifications
from fund_alert_bot.notifications.service import build_notification_service

if TYPE_CHECKING:
    from telegram.ext import Application

LOGGER = logging.getLogger(__name__)

DEFAULT_AFTER_CLOSE_CHECK_TIME = "17:10"
DEFAULT_BEFORE_CLOSE_CHECK_TIME = "14:50"
DEFAULT_DCA_REMINDER_TIME = "09:30"
DEFAULT_FUND_NAV_PROCESS_TIME = "08:30"
MARKET_AFTER_CLOSE_JOB_ID = "market-after-close-check"
MARKET_BEFORE_CLOSE_JOB_ID = "market-before-close-check"
DRAW_DOWN_AFTER_CLOSE_JOB_ID = MARKET_AFTER_CLOSE_JOB_ID
DCA_MORNING_JOB_ID = "dca-morning-reminder-check"
FUND_NAV_PROCESS_JOB_ID = "fund-nav-process"
WEEKDAY_CRON_FILTER = "mon-fri"


def create_scheduler(*, timezone: str) -> Any:
    """Create an APScheduler instance for the Telegram event loop."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    return AsyncIOScheduler(timezone=timezone)


def parse_after_close_check_time(raw_value: str) -> time:
    """Parse AFTER_CLOSE_CHECK_TIME as HH:MM."""

    return _parse_hhmm_time(raw_value, name="AFTER_CLOSE_CHECK_TIME")


def parse_before_close_check_time(raw_value: str) -> time:
    """Parse BEFORE_CLOSE_CHECK_TIME as HH:MM."""

    return _parse_hhmm_time(raw_value, name="BEFORE_CLOSE_CHECK_TIME")


def parse_dca_reminder_time(raw_value: str) -> time:
    """Parse DCA_REMINDER_TIME as HH:MM."""

    return _parse_hhmm_time(raw_value, name="DCA_REMINDER_TIME")


def parse_fund_nav_process_time(raw_value: str) -> time:
    """Parse FUND_NAV_PROCESS_TIME as HH:MM."""

    return _parse_hhmm_time(raw_value, name="FUND_NAV_PROCESS_TIME")


def _parse_hhmm_time(raw_value: str, *, name: str) -> time:
    pieces = raw_value.strip().split(":")
    if len(pieces) != 2:
        raise ValueError(f"{name} must use HH:MM")

    raw_hour, raw_minute = pieces
    try:
        hour = int(raw_hour)
        minute = int(raw_minute)
    except ValueError as exc:
        raise ValueError(f"{name} must use HH:MM") from exc

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError(f"{name} must be a valid 24-hour time")

    return time(hour=hour, minute=minute)


def create_weekday_after_close_trigger(
    *,
    check_time: time,
    timezone: str | tzinfo,
) -> Any:
    """Build the Monday-Friday after-close CronTrigger."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        day_of_week=WEEKDAY_CRON_FILTER,
        hour=check_time.hour,
        minute=check_time.minute,
        timezone=timezone,
    )


def create_daily_dca_trigger(
    *,
    reminder_time: time,
    timezone: str | tzinfo,
) -> Any:
    """Build the daily DCA reminder CronTrigger."""
    from apscheduler.triggers.cron import CronTrigger

    return CronTrigger(
        hour=reminder_time.hour,
        minute=reminder_time.minute,
        timezone=timezone,
    )


def register_jobs(
    scheduler: Any,
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    timezone: str,
    check_time: str = DEFAULT_AFTER_CLOSE_CHECK_TIME,
    before_close_check_time: str = DEFAULT_BEFORE_CLOSE_CHECK_TIME,
    dca_reminder_time: str = DEFAULT_DCA_REMINDER_TIME,
    fund_nav_process_time: str = DEFAULT_FUND_NAV_PROCESS_TIME,
    market_data_provider: MarketDataProvider | None = None,
    market_calendar: MarketCalendar | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Register scheduled alert jobs."""

    parsed_time = parse_after_close_check_time(check_time)
    parsed_before_close_time = parse_before_close_check_time(before_close_check_time)
    parsed_dca_time = parse_dca_reminder_time(dca_reminder_time)
    parsed_fund_nav_time = parse_fund_nav_process_time(fund_nav_process_time)
    if market_data_provider is None:
        market_data_provider = AkshareMarketDataProvider()
    if market_calendar is None:
        market_calendar = CNMarketCalendar()

    scheduler.add_job(
        run_scheduled_market_check,
        trigger=create_weekday_after_close_trigger(
            check_time=parsed_time,
            timezone=timezone,
        ),
        id=MARKET_AFTER_CLOSE_JOB_ID,
        name="Market after-close reminder check",
        kwargs={
            "application": application,
            "sqlite_path": sqlite_path,
            "allowed_user_ids": frozenset(allowed_user_ids),
            "market_data_provider": market_data_provider,
            "market_calendar": market_calendar,
            "timezone": timezone,
            "notification_settings": notification_settings,
        },
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    LOGGER.info(
        "Registered scheduled market reminder check for %s at %s %s",
        WEEKDAY_CRON_FILTER,
        parsed_time.strftime("%H:%M"),
        timezone,
    )

    scheduler.add_job(
        run_scheduled_fund_nav_process,
        trigger=create_daily_dca_trigger(
            reminder_time=parsed_fund_nav_time,
            timezone=timezone,
        ),
        id=FUND_NAV_PROCESS_JOB_ID,
        name="Feeder-fund exact-date NAV processing",
        kwargs={
            "application": application,
            "sqlite_path": sqlite_path,
            "allowed_user_ids": frozenset(allowed_user_ids),
            "market_data_provider": market_data_provider,
            "market_calendar": market_calendar,
            "timezone": timezone,
            "notification_settings": notification_settings,
        },
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    LOGGER.info(
        "Registered feeder-fund NAV processing daily at %s %s",
        parsed_fund_nav_time.strftime("%H:%M"),
        timezone,
    )

    scheduler.add_job(
        run_scheduled_before_close_check,
        trigger=create_weekday_after_close_trigger(
            check_time=parsed_before_close_time,
            timezone=timezone,
        ),
        id=MARKET_BEFORE_CLOSE_JOB_ID,
        name="Market before-close realtime drawdown check",
        kwargs={
            "application": application,
            "sqlite_path": sqlite_path,
            "allowed_user_ids": frozenset(allowed_user_ids),
            "market_data_provider": market_data_provider,
            "market_calendar": market_calendar,
            "timezone": timezone,
            "notification_settings": notification_settings,
        },
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=1800,
    )
    LOGGER.info(
        "Registered scheduled realtime drawdown check for %s at %s %s",
        WEEKDAY_CRON_FILTER,
        parsed_before_close_time.strftime("%H:%M"),
        timezone,
    )

    scheduler.add_job(
        run_scheduled_dca_check,
        trigger=create_daily_dca_trigger(
            reminder_time=parsed_dca_time,
            timezone=timezone,
        ),
        id=DCA_MORNING_JOB_ID,
        name="DCA morning reminder check",
        kwargs={
            "application": application,
            "sqlite_path": sqlite_path,
            "allowed_user_ids": frozenset(allowed_user_ids),
            "timezone": timezone,
            "market_calendar": market_calendar,
            "notification_settings": notification_settings,
        },
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    LOGGER.info(
        "Registered scheduled DCA reminder check daily at %s %s",
        parsed_dca_time.strftime("%H:%M"),
        timezone,
    )


async def run_scheduled_before_close_check(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    market_data_provider: MarketDataProvider,
    timezone: str | tzinfo,
    market_calendar: MarketCalendar | None = None,
    run_date: date | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Run a before-close realtime drawdown check and send notifications."""

    check_date = run_date or _current_date(timezone)
    LOGGER.info(
        "Scheduled realtime drawdown check started date=%s",
        check_date.isoformat(),
    )
    drawdown_result = None
    plan_result = None
    try:
        if market_calendar is None:
            market_calendar = CNMarketCalendar()
        if not market_calendar.is_trading_day(check_date):
            LOGGER.info(
                "Scheduled realtime drawdown check skipped date=%s "
                "reason=market_closed",
                check_date.isoformat(),
            )
            return

        try:
            confirmed_end_date = latest_completed_open_date(
                market_calendar,
                check_date,
            )
        except MarketCalendarUnavailableError as exc:
            LOGGER.warning(
                "Skipping realtime drawdown row because the confirmed market "
                "date is unavailable: %s",
                exc,
            )
            confirmed_end_date = None

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            if confirmed_end_date is None:
                drawdown_result = DrawdownCheckResult(0, [], 0, [], [])
            else:
                drawdown_result = evaluate_drawdown_rules(
                    connection,
                    market_data_provider,
                    today=check_date,
                    require_new_data_date=check_date,
                    include_latest=True,
                    confirmed_end_date=confirmed_end_date,
                )
            plan_rules = [
                row
                for row in list_enabled_rules(connection)
                if row["type"] == "drawdown_plan"
            ]
            try:
                confirmed_plan_day = bool(
                    plan_rules
                ) and market_calendar.confirmed_status(check_date)
                confirmed_date = confirmed_end_date if confirmed_plan_day else None
            except MarketCalendarUnavailableError as exc:
                plan_result = DrawdownPlanCheckResult(
                    checked_rules=len(plan_rules),
                    notifications=[],
                    no_data_skips=[
                        RuleNoDataSkip(
                            rule_id=int(row["id"]),
                            symbol=str(row["symbol"]),
                            message=str(exc),
                        )
                        for row in plan_rules
                    ],
                    errors=[],
                )
            else:
                plan_result = (
                    evaluate_drawdown_plan_prealerts(
                        connection,
                        market_data_provider,
                        market_date=check_date,
                        confirmed_date=confirmed_date,
                    )
                    if confirmed_date is not None
                    else DrawdownPlanCheckResult(len(plan_rules), [], [], [])
                )
            data_notice = reserve_drawdown_plan_data_unavailable_notice(
                connection,
                evaluation_date=check_date,
                result=plan_result,
                phase="before_close",
            )

        for status in drawdown_result.statuses:
            LOGGER.info(
                "Realtime drawdown status rule_id=%s symbol=%s drawdown=%.2f%% "
                "latest_price=%s latest_date=%s peak_price=%s peak_date=%s",
                status.rule_id,
                status.symbol,
                status.drawdown * 100,
                status.latest_price,
                status.latest_date,
                status.peak_price,
                status.peak_date,
            )
        for skip in [
            *drawdown_result.no_data_skips,
            *plan_result.no_data_skips,
        ]:
            LOGGER.info(
                "Scheduled realtime drawdown check skipped rule_id=%s symbol=%s: %s",
                skip.rule_id,
                skip.symbol,
                skip.message,
            )
        for error in [*drawdown_result.errors, *plan_result.errors]:
            LOGGER.warning(
                "Scheduled realtime drawdown check error rule_id=%s symbol=%s: %s",
                error.rule_id,
                error.symbol,
                error.message,
            )

        await send_scheduled_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            notifications=[
                *drawdown_result.notifications,
                *plan_result.notifications,
                *([] if data_notice is None else [data_notice]),
            ],
            notification_settings=notification_settings,
        )
    except Exception:
        LOGGER.exception("Scheduled realtime drawdown check failed")
        raise
    finally:
        if drawdown_result is None or plan_result is None:
            LOGGER.info("Scheduled realtime drawdown check ended")
        else:
            LOGGER.info(
                "Scheduled realtime drawdown check ended: drawdown_rules=%d "
                "plan_rules=%d "
                "new_alerts=%d duplicate_alerts=%d no_data_skips=%d errors=%d",
                drawdown_result.checked_rules,
                plan_result.checked_rules,
                len(drawdown_result.notifications) + len(plan_result.notifications),
                drawdown_result.skipped_duplicates,
                len(drawdown_result.no_data_skips) + len(plan_result.no_data_skips),
                len(drawdown_result.errors) + len(plan_result.errors),
            )


async def run_scheduled_market_check(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    market_data_provider: MarketDataProvider,
    timezone: str | tzinfo,
    market_calendar: MarketCalendar | None = None,
    run_date: date | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Run scheduled after-close market reminders and send notifications."""

    check_date = run_date or _current_date(timezone)
    LOGGER.info(
        "Scheduled market reminder check started for date=%s",
        check_date.isoformat(),
    )
    drawdown_result = None
    plan_result = None
    profit_result = None
    try:
        await retry_pending_drawdown_plan_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            action_date=check_date,
            notification_settings=notification_settings,
        )
        await retry_pending_position_profit_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            notification_settings=notification_settings,
        )
        if market_calendar is None:
            market_calendar = CNMarketCalendar()
        if not market_calendar.is_trading_day(check_date):
            LOGGER.info(
                "Scheduled market reminder check skipped for date=%s: "
                "CN market is not trading.",
                check_date.isoformat(),
            )
            return

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            drawdown_result = evaluate_drawdown_rules(
                connection,
                market_data_provider,
                today=check_date,
                require_new_data_date=check_date,
            )
            plan_rules = [
                row
                for row in list_enabled_rules(connection)
                if row["type"] == "drawdown_plan"
            ]
            try:
                confirmed_plan_day = bool(
                    plan_rules
                ) and market_calendar.confirmed_status(check_date)
            except MarketCalendarUnavailableError as exc:
                plan_result = DrawdownPlanCheckResult(
                    checked_rules=len(plan_rules),
                    notifications=[],
                    no_data_skips=[
                        RuleNoDataSkip(
                            rule_id=int(row["id"]),
                            symbol=str(row["symbol"]),
                            message=str(exc),
                        )
                        for row in plan_rules
                    ],
                    errors=[],
                )
            else:
                plan_result = (
                    evaluate_drawdown_plan_rules(
                        connection,
                        market_data_provider,
                        expected_date=check_date,
                    )
                    if confirmed_plan_day
                    else DrawdownPlanCheckResult(len(plan_rules), [], [], [])
                )
            profit_result = evaluate_profit_rules(connection, market_data_provider)
            data_notice = reserve_drawdown_plan_data_unavailable_notice(
                connection,
                evaluation_date=check_date,
                result=plan_result,
            )

        for skip in [
            *drawdown_result.no_data_skips,
            *plan_result.no_data_skips,
            *profit_result.no_data_skips,
        ]:
            LOGGER.info(
                "Scheduled market reminder check skipped rule_id=%s symbol=%s: %s",
                skip.rule_id,
                skip.symbol,
                skip.message,
            )
        for error in [
            *drawdown_result.errors,
            *plan_result.errors,
            *profit_result.errors,
        ]:
            LOGGER.warning(
                "Scheduled market reminder check error rule_id=%s symbol=%s: %s",
                error.rule_id,
                error.symbol,
                error.message,
            )

        await send_scheduled_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            notifications=[
                *drawdown_result.notifications,
                *plan_result.notifications,
                *profit_result.notifications,
                *([] if data_notice is None else [data_notice]),
            ],
            notification_settings=notification_settings,
        )
    except Exception:
        LOGGER.exception("Scheduled market reminder check failed")
        raise
    finally:
        if drawdown_result is None or plan_result is None or profit_result is None:
            LOGGER.info("Scheduled market reminder check ended")
        else:
            LOGGER.info(
                "Scheduled market reminder check ended: "
                "drawdown_rules=%d plan_rules=%d profit_rules=%d new_alerts=%d "
                "duplicate_alerts=%d no_data_skips=%d errors=%d",
                drawdown_result.checked_rules,
                plan_result.checked_rules,
                profit_result.checked_rules,
                len(drawdown_result.notifications)
                + len(plan_result.notifications)
                + len(profit_result.notifications),
                drawdown_result.skipped_duplicates + profit_result.skipped_duplicates,
                len(drawdown_result.no_data_skips)
                + len(plan_result.no_data_skips)
                + len(profit_result.no_data_skips),
                len(drawdown_result.errors)
                + len(plan_result.errors)
                + len(profit_result.errors),
            )


async def run_scheduled_drawdown_check(
    **kwargs: Any,
) -> None:
    """Backward-compatible wrapper for the after-close market reminder job."""

    await run_scheduled_market_check(**kwargs)


async def run_scheduled_fund_nav_process(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    market_data_provider: MarketDataProvider,
    market_calendar: MarketCalendar,
    timezone: str | tzinfo,
    run_date: date | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Settle pending manual additions from exact-date feeder-fund NAVs."""

    processing_date = run_date or _current_date(timezone)
    LOGGER.info("Feeder-fund NAV processing started date=%s", processing_date)
    result = None
    try:
        await retry_pending_standard_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            notification_settings=notification_settings,
        )
        await retry_pending_drawdown_plan_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            action_date=processing_date,
            notification_settings=notification_settings,
        )
        await retry_pending_position_profit_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            notification_settings=notification_settings,
        )
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            nav_cache: dict[tuple[str, date], Any] = {}
            nav_errors: dict[tuple[str, date], Exception] = {}
            dca_result = process_scheduled_dca_occurrences(
                connection,
                market_data_provider,
                market_calendar,
                processing_date=processing_date,
                nav_cache=nav_cache,
                nav_errors=nav_errors,
            )
            manual_result = process_manual_add_estimates(
                connection,
                market_data_provider,
                market_calendar,
                processing_date=processing_date,
                nav_cache=nav_cache,
                nav_errors=nav_errors,
            )
            position_profit_result = evaluate_position_profit_rules(
                connection,
                market_data_provider,
                market_calendar,
                processing_date=processing_date,
                nav_cache=nav_cache,
                nav_errors=nav_errors,
            )
            settlement_result = ManualAddSettlementResult(
                checked_estimates=(
                    dca_result.checked_estimates + manual_result.checked_estimates
                ),
                notifications=manual_result.notifications,
                no_data_skips=[
                    *dca_result.no_data_skips,
                    *manual_result.no_data_skips,
                ],
                errors=[*dca_result.errors, *manual_result.errors],
            )
            result = ManualAddSettlementResult(
                checked_estimates=settlement_result.checked_estimates,
                notifications=settlement_result.notifications,
                no_data_skips=[
                    *settlement_result.no_data_skips,
                    *position_profit_result.no_data_skips,
                ],
                errors=[*settlement_result.errors, *position_profit_result.errors],
            )
            affected_by_date: dict[date, list[Any]] = {}
            for item in [*result.no_data_skips, *result.errors]:
                affected_by_date.setdefault(
                    item.data_date or processing_date, []
                ).append(item)
            fund_nav_notices = []
            for data_date, affected in sorted(affected_by_date.items()):
                notice = reserve_drawdown_plan_data_unavailable_notice(
                    connection,
                    evaluation_date=data_date,
                    result=ManualAddSettlementResult(0, [], affected, []),
                    phase="fund_nav",
                )
                if notice is not None:
                    fund_nav_notices.append(notice)
        for skip in result.no_data_skips:
            LOGGER.info(
                "Fund NAV unavailable rule_id=%s symbol=%s: %s",
                skip.rule_id,
                skip.symbol,
                skip.message,
            )
        for error in result.errors:
            LOGGER.warning(
                "Fund NAV processing error rule_id=%s symbol=%s: %s",
                error.rule_id,
                error.symbol,
                error.message,
            )
        await send_scheduled_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            notifications=[
                *result.notifications,
                *position_profit_result.notifications,
                *fund_nav_notices,
            ],
            notification_settings=notification_settings,
        )
    except Exception:
        LOGGER.exception("Feeder-fund NAV processing failed")
        raise
    finally:
        if result is None:
            LOGGER.info("Feeder-fund NAV processing ended")
        else:
            LOGGER.info(
                "Feeder-fund NAV processing ended checked=%d applied=%d "
                "no_data=%d errors=%d",
                result.checked_estimates,
                len(result.notifications),
                len(result.no_data_skips),
                len(result.errors),
            )


async def run_scheduled_dca_check(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    timezone: str | tzinfo,
    market_calendar: MarketCalendar | None = None,
    run_date: date | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Run the scheduled DCA reminder check and send due notifications."""

    check_date = run_date or _current_date(timezone)
    LOGGER.info("Scheduled DCA reminder check started for date=%s", check_date)
    result = None
    try:
        await retry_pending_drawdown_plan_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            action_date=check_date,
            notification_settings=notification_settings,
        )
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            result = evaluate_dca_rules(
                connection,
                today=check_date,
                market_calendar=market_calendar,
            )

        for error in result.errors:
            LOGGER.warning(
                "Scheduled DCA reminder check error rule_id=%s symbol=%s: %s",
                error.rule_id,
                error.symbol,
                error.message,
            )

        await send_scheduled_notifications(
            application=application,
            sqlite_path=sqlite_path,
            allowed_user_ids=allowed_user_ids,
            notifications=result.notifications,
            notification_settings=notification_settings,
        )
    except Exception:
        LOGGER.exception("Scheduled DCA reminder check failed")
        raise
    finally:
        if result is None:
            LOGGER.info("Scheduled DCA reminder check ended")
        else:
            LOGGER.info(
                "Scheduled DCA reminder check ended: checked_rules=%d "
                "new_alerts=%d duplicate_alerts=%d errors=%d",
                result.checked_rules,
                len(result.notifications),
                result.skipped_duplicates,
                len(result.errors),
            )


async def send_scheduled_notifications(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    notifications: list[AlertNotification],
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Send scheduled alert notifications to enabled channels."""

    if not notifications:
        return

    notification_service = build_notification_service(
        settings=notification_settings,
        telegram_bot=application.bot,
        telegram_chat_ids=allowed_user_ids,
    )
    dispatch_summary = await send_alert_notifications(
        sqlite_path=sqlite_path,
        notification_service=notification_service,
        notifications=notifications,
    )
    if dispatch_summary.failed:
        LOGGER.warning(
            "Scheduled notification delivery failures: %d",
            dispatch_summary.failed,
        )


async def retry_pending_drawdown_plan_notifications(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    action_date: date,
    notification_settings: NotificationSettings | None = None,
) -> int:
    """Retry durable plan reminders independently of market-day evaluation."""

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        notifications = [
            AlertNotification(
                event_id=int(row["id"]),
                title=str(row["title"]),
                text=(
                    format_delayed_drawdown_plan_message(str(row["message"]))
                    if row["data_date"] is not None
                    and str(row["data_date"]) < action_date.isoformat()
                    else str(row["message"])
                ),
            )
            for row in list_retryable_drawdown_plan_alert_events(connection)
        ]
    await send_scheduled_notifications(
        application=application,
        sqlite_path=sqlite_path,
        allowed_user_ids=allowed_user_ids,
        notifications=notifications,
        notification_settings=notification_settings,
    )
    return len(notifications)


async def retry_pending_standard_notifications(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    notification_settings: NotificationSettings | None = None,
) -> int:
    """Retry durable standard reminders after delivery failure or restart."""

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        notifications = [
            AlertNotification(
                event_id=int(row["id"]),
                title=str(row["title"]),
                text=str(row["message"]),
                telegram_actions=(
                    (
                        (
                            f"⚠️ Deduction failed — {row['fund_symbol']}",
                            f"dca_skip:{row['rule_id']}:{row['due_date']}",
                        ),
                    ),
                )
                if row["rule_type"] == "dca_reminder"
                and row["due_date"] is not None
                and row["occurrence_status"] == "pending"
                else (),
                dca_summary=(
                    build_dca_notification_summary(
                        message=str(row["message"]),
                        due_date=str(row["due_date"]),
                        amount=float(row["dca_amount"]),
                        skipped=str(row["occurrence_status"]) == "skipped",
                        current_status=str(row["occurrence_status"]),
                        current_effective_date=(
                            None
                            if row["occurrence_effective_date"] is None
                            else str(row["occurrence_effective_date"])
                        ),
                    )
                    if row["rule_type"] == "dca_reminder"
                    and row["fund_symbol"] is not None
                    and row["dca_amount"] is not None
                    and row["occurrence_status"] is not None
                    else None
                ),
            )
            for row in list_retryable_standard_alert_events(connection)
        ]
    await send_scheduled_notifications(
        application=application,
        sqlite_path=sqlite_path,
        allowed_user_ids=allowed_user_ids,
        notifications=notifications,
        notification_settings=notification_settings,
    )
    return len(notifications)


async def retry_pending_position_profit_notifications(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    notification_settings: NotificationSettings | None = None,
) -> int:
    """Retry durable position-linked Price-Gain reminders."""

    with open_connection(sqlite_path) as connection:
        initialize_database(connection)
        notifications = [
            AlertNotification(
                event_id=int(row["id"]),
                title=str(row["title"]),
                text=str(row["message"]),
                telegram_actions=position_profit_action_rows(int(row["id"])),
            )
            for row in list_retryable_position_profit_alert_events(connection)
        ]
    await send_scheduled_notifications(
        application=application,
        sqlite_path=sqlite_path,
        allowed_user_ids=allowed_user_ids,
        notifications=notifications,
        notification_settings=notification_settings,
    )
    return len(notifications)


def _current_date(timezone: str | tzinfo) -> date:
    """Return today's date in the configured scheduler timezone."""

    if isinstance(timezone, str):
        timezone = ZoneInfo(timezone)
    return datetime.now(tz=timezone).date()
