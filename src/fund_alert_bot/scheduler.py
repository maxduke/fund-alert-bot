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
    evaluate_dca_rules,
    evaluate_drawdown_rules,
    evaluate_profit_rules,
)
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import initialize_database, open_connection
from fund_alert_bot.market_data import (
    AkshareMarketDataProvider,
    CNMarketCalendar,
    MarketCalendar,
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
MARKET_AFTER_CLOSE_JOB_ID = "market-after-close-check"
MARKET_BEFORE_CLOSE_JOB_ID = "market-before-close-check"
DRAW_DOWN_AFTER_CLOSE_JOB_ID = MARKET_AFTER_CLOSE_JOB_ID
DCA_MORNING_JOB_ID = "dca-morning-reminder-check"
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
    market_data_provider: MarketDataProvider | None = None,
    market_calendar: MarketCalendar | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Register scheduled alert jobs."""

    parsed_time = parse_after_close_check_time(check_time)
    parsed_before_close_time = parse_before_close_check_time(before_close_check_time)
    parsed_dca_time = parse_dca_reminder_time(dca_reminder_time)
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
    result = None
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

        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            result = evaluate_drawdown_rules(
                connection,
                market_data_provider,
                today=check_date,
                require_new_data_date=check_date,
                include_latest=True,
            )

        for status in result.statuses:
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
        for skip in result.no_data_skips:
            LOGGER.info(
                "Scheduled realtime drawdown check skipped rule_id=%s symbol=%s: %s",
                skip.rule_id,
                skip.symbol,
                skip.message,
            )
        for error in result.errors:
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
            notifications=result.notifications,
            notification_settings=notification_settings,
        )
    except Exception:
        LOGGER.exception("Scheduled realtime drawdown check failed")
        raise
    finally:
        if result is None:
            LOGGER.info("Scheduled realtime drawdown check ended")
        else:
            LOGGER.info(
                "Scheduled realtime drawdown check ended: checked_rules=%d "
                "new_alerts=%d duplicate_alerts=%d no_data_skips=%d errors=%d",
                result.checked_rules,
                len(result.notifications),
                result.skipped_duplicates,
                len(result.no_data_skips),
                len(result.errors),
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
    profit_result = None
    try:
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
            profit_result = evaluate_profit_rules(connection, market_data_provider)

        for skip in [*drawdown_result.no_data_skips, *profit_result.no_data_skips]:
            LOGGER.info(
                "Scheduled market reminder check skipped rule_id=%s symbol=%s: %s",
                skip.rule_id,
                skip.symbol,
                skip.message,
            )
        for error in [*drawdown_result.errors, *profit_result.errors]:
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
                *profit_result.notifications,
            ],
            notification_settings=notification_settings,
        )
    except Exception:
        LOGGER.exception("Scheduled market reminder check failed")
        raise
    finally:
        if drawdown_result is None or profit_result is None:
            LOGGER.info("Scheduled market reminder check ended")
        else:
            LOGGER.info(
                "Scheduled market reminder check ended: "
                "drawdown_rules=%d profit_rules=%d new_alerts=%d "
                "duplicate_alerts=%d no_data_skips=%d errors=%d",
                drawdown_result.checked_rules,
                profit_result.checked_rules,
                len(drawdown_result.notifications) + len(profit_result.notifications),
                drawdown_result.skipped_duplicates + profit_result.skipped_duplicates,
                len(drawdown_result.no_data_skips) + len(profit_result.no_data_skips),
                len(drawdown_result.errors) + len(profit_result.errors),
            )


async def run_scheduled_drawdown_check(
    **kwargs: Any,
) -> None:
    """Backward-compatible wrapper for the after-close market reminder job."""

    await run_scheduled_market_check(**kwargs)


async def run_scheduled_dca_check(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    timezone: str | tzinfo,
    run_date: date | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Run the scheduled DCA reminder check and send due notifications."""

    check_date = run_date or _current_date(timezone)
    LOGGER.info("Scheduled DCA reminder check started for date=%s", check_date)
    result = None
    try:
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            result = evaluate_dca_rules(connection, today=check_date)

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


def _current_date(timezone: str | tzinfo) -> date:
    """Return today's date in the configured scheduler timezone."""

    if isinstance(timezone, str):
        timezone = ZoneInfo(timezone)
    return datetime.now(tz=timezone).date()
