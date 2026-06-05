"""Scheduler wiring."""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import date, datetime, time, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fund_alert_bot.checks import AlertNotification, evaluate_drawdown_rules
from fund_alert_bot.config import NotificationSettings
from fund_alert_bot.db import initialize_database, open_connection
from fund_alert_bot.market_data import AkshareMarketDataProvider, MarketDataProvider
from fund_alert_bot.notifications.service import build_notification_service

if TYPE_CHECKING:
    from telegram.ext import Application

LOGGER = logging.getLogger(__name__)

DEFAULT_AFTER_CLOSE_CHECK_TIME = "17:10"
DRAW_DOWN_AFTER_CLOSE_JOB_ID = "drawdown-after-close-check"
WEEKDAY_CRON_FILTER = "mon-fri"


def create_scheduler(*, timezone: str) -> Any:
    """Create an APScheduler instance for the Telegram event loop."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    return AsyncIOScheduler(timezone=timezone)


def parse_after_close_check_time(raw_value: str) -> time:
    """Parse AFTER_CLOSE_CHECK_TIME as HH:MM."""

    pieces = raw_value.strip().split(":")
    if len(pieces) != 2:
        raise ValueError("AFTER_CLOSE_CHECK_TIME must use HH:MM")

    raw_hour, raw_minute = pieces
    try:
        hour = int(raw_hour)
        minute = int(raw_minute)
    except ValueError as exc:
        raise ValueError("AFTER_CLOSE_CHECK_TIME must use HH:MM") from exc

    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("AFTER_CLOSE_CHECK_TIME must be a valid 24-hour time")

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


def register_jobs(
    scheduler: Any,
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    timezone: str,
    check_time: str = DEFAULT_AFTER_CLOSE_CHECK_TIME,
    market_data_provider: MarketDataProvider | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Register scheduled drawdown jobs."""

    parsed_time = parse_after_close_check_time(check_time)
    if market_data_provider is None:
        market_data_provider = AkshareMarketDataProvider()

    scheduler.add_job(
        run_scheduled_drawdown_check,
        trigger=create_weekday_after_close_trigger(
            check_time=parsed_time,
            timezone=timezone,
        ),
        id=DRAW_DOWN_AFTER_CLOSE_JOB_ID,
        name="Drawdown after-close check",
        kwargs={
            "application": application,
            "sqlite_path": sqlite_path,
            "allowed_user_ids": frozenset(allowed_user_ids),
            "market_data_provider": market_data_provider,
            "timezone": timezone,
            "notification_settings": notification_settings,
        },
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    LOGGER.info(
        "Registered scheduled drawdown check for %s at %s %s",
        WEEKDAY_CRON_FILTER,
        parsed_time.strftime("%H:%M"),
        timezone,
    )


async def run_scheduled_drawdown_check(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
    sqlite_path: str | Path,
    allowed_user_ids: Collection[int],
    market_data_provider: MarketDataProvider,
    timezone: str | tzinfo,
    run_date: date | None = None,
    notification_settings: NotificationSettings | None = None,
) -> None:
    """Run the scheduled drawdown check and send new alert notifications."""

    check_date = run_date or _current_date(timezone)
    LOGGER.info("Scheduled drawdown check started for date=%s", check_date.isoformat())
    result = None
    try:
        with open_connection(sqlite_path) as connection:
            initialize_database(connection)
            result = evaluate_drawdown_rules(
                connection,
                market_data_provider,
                today=check_date,
                require_new_data_date=check_date,
            )

        for skip in result.no_data_skips:
            LOGGER.info(
                "Scheduled drawdown check skipped rule_id=%s symbol=%s: %s",
                skip.rule_id,
                skip.symbol,
                skip.message,
            )
        for error in result.errors:
            LOGGER.warning(
                "Scheduled drawdown check error rule_id=%s symbol=%s: %s",
                error.rule_id,
                error.symbol,
                error.message,
            )

        await send_scheduled_notifications(
            application=application,
            allowed_user_ids=allowed_user_ids,
            notifications=result.notifications,
            notification_settings=notification_settings,
        )
    except Exception:
        LOGGER.exception("Scheduled drawdown check failed")
        raise
    finally:
        if result is None:
            LOGGER.info("Scheduled drawdown check ended")
        else:
            LOGGER.info(
                "Scheduled drawdown check ended: checked_rules=%d new_alerts=%d "
                "duplicate_alerts=%d no_data_skips=%d errors=%d",
                result.checked_rules,
                len(result.notifications),
                result.skipped_duplicates,
                len(result.no_data_skips),
                len(result.errors),
            )


async def send_scheduled_notifications(
    *,
    application: Application[Any, Any, Any, Any, Any, Any],
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
    for notification in notifications:
        await notification_service.send_alert(
            title=notification.title,
            body=notification.text,
        )


def _current_date(timezone: str | tzinfo) -> date:
    """Return today's date in the configured scheduler timezone."""

    if isinstance(timezone, str):
        timezone = ZoneInfo(timezone)
    return datetime.now(tz=timezone).date()
